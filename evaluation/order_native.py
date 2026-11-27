"""Prepare the isolated Maven toolchain and verify Java candidate images."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import re
import subprocess

from evaluation.order_release_fixture import dual_order_probe, order_probe


MAVEN_IMAGE = "maven@sha256:99e61abcff91a9b1333463bd8451fb18495d6eba9250ac66a338b518f8278320"
MAVEN_CACHE_REPOSITORY = "liveprobe-eval/order-maven"
ORDER_PROBE_SOURCE = r"""
import com.sun.net.httpserver.HttpServer;
import java.net.*;
import java.net.http.*;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.Base64;
import java.util.UUID;
import java.util.regex.Pattern;

public class OrderProbe {
    public static void main(String[] args) throws Exception {
        var inventory = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        inventory.createContext("/api/stock/", exchange -> {
            var item = exchange.getRequestURI().getPath().substring("/api/stock/".length());
            var payload = switch (item) {
                case "a" -> "{\"item\":\"a\",\"quantity\":7,\"available\":true}";
                case "b" -> "{\"item\":\"b\",\"quantity\":0,\"available\":false}";
                default -> "{\"error\":\"unknown item\"}";
            };
            byte[] body = payload.getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type", "application/json");
            exchange.sendResponseHeaders(item.equals("a") || item.equals("b") ? 200 : 404, body.length);
            try (var stream = exchange.getResponseBody()) {
                stream.write(body);
            }
        });
        inventory.start();
        var runner = new ProcessBuilder("java", "-Xmx192m", "-XX:ActiveProcessorCount=2", "-jar", "/app/app.jar")
            .redirectOutput(ProcessBuilder.Redirect.DISCARD).redirectError(ProcessBuilder.Redirect.DISCARD);
        var env = runner.environment();
        var token = UUID.randomUUID().toString();
        env.put("SPRING_PROFILES_ACTIVE", "chaos");
        env.put("CHAOS_OPERATOR_TOKEN", token);
        env.put("CHAOS_STATE_DIR", "/tmp/order-state");
        env.put("SERVER_ADDRESS", "127.0.0.1");
        env.put("SERVER_PORT", "18080");
        env.put("ORDER_DB_ENABLED", "false");
        env.put("SELF_TRAFFIC_INTERVAL_SECONDS", "0");
        env.put("RELEASE_FAULTS", "{\"faults\":{}}");
        env.put("INVENTORY_URL", "http://127.0.0.1:" + inventory.getAddress().getPort());
        var app = runner.start();
        var client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(1)).build();
        var phase = "startup";
        try {
            var deadline = System.nanoTime() + Duration.ofSeconds(60).toNanos();
            while (true) {
                try {
                    if (get(client, "/actuator/health/readiness", token).statusCode() == 200)
                        break;
                } catch (java.io.IOException ignored) {
                    // The application may still be binding its listener.
                }
                if (!app.isAlive() || System.nanoTime() >= deadline)
                    throw new IllegalStateException("readiness");
                Thread.sleep(100);
            }
            System.out.println("LP_ORDER_RUNTIME " + System.getProperty("java.version"));
            phase = "runtime_configuration";
            print(client, "/internal/chaos/v1/app-faults", token);
            phase = "business";
            for (int i = 0; i < 2; i++) {
                for (var path : new String[]{"/actuator/health/readiness", "/api/orders/check?item=a",
                        "/api/orders/check?item=b", "/api/orders/check?item=missing"})
                    print(client, path, token);
                if (args.length > 0 && args[0].equals("dual")) {
                    var created = post(client, "{\"item\":\"a\"}");
                    print("create", created);
                    var id = Pattern.compile("\"id\"\\s*:\\s*(\\d+)").matcher(created.body());
                    if (created.statusCode() == 201 && id.find()) {
                        print("stored", get(client, "/api/orders/" + id.group(1), token));
                    } else {
                        print("stored", 0, "{}");
                    }
                    print("seed", get(client, "/api/orders/1", token));
                    print("invalid", post(client, "{\"item\":\"\"}"));
                }
            }
        } catch (Exception error) {
            System.out.println("LP_ORDER_FAILURE " + phase + " " + error.getClass().getSimpleName());
            throw error;
        } finally {
            app.destroyForcibly();
            inventory.stop(0);
        }
    }
    private static HttpResponse<String> get(HttpClient client, String path, String token) throws Exception {
        var request = HttpRequest.newBuilder(URI.create("http://127.0.0.1:18080" + path))
            .timeout(Duration.ofSeconds(3));
        if (path.startsWith("/internal/"))
            request.header("Authorization", "Bearer " + token);
        return client.send(request.GET().build(), HttpResponse.BodyHandlers.ofString());
    }
    private static void print(HttpClient client, String path, String token) throws Exception {
        print(path, get(client, path, token));
    }
    private static HttpResponse<String> post(HttpClient client, String body) throws Exception {
        var request = HttpRequest.newBuilder(URI.create("http://127.0.0.1:18080/api/orders"))
            .timeout(Duration.ofSeconds(3)).header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(body)).build();
        return client.send(request, HttpResponse.BodyHandlers.ofString());
    }
    private static void print(String path, HttpResponse<String> response) {
        print(path, response.statusCode(), response.body());
    }
    private static void print(String path, int code, String response) {
        var body = Base64.getEncoder().encodeToString(response.getBytes(StandardCharsets.UTF_8));
        System.out.println("LP_ORDER_HTTP " + path + " " + code + " " + body);
    }
}
"""


def prepare_order_toolchain(host, workspace, trial):
    context = workspace / ("java-cache-" + workspace.name)
    context.mkdir()
    result = host.invoke(["run", "--rm", "--pull=never", "--network=none", "--memory=192m", "--cpus=0.5",
                          "--mount", "type=volume,src=liveprobe-m2,dst=/cache,readonly",
                          "--mount", f"type=bind,src={context},dst=/out",
                          "--entrypoint", "sh", MAVEN_IMAGE, "-eu", "-c",
                          r"cd /cache && find repository -type f \( -name '*.jar' -o -name '*.pom' "
                          r"-o -name '_remote.repositories' \) -print0 | tar --null -cf - -T - | tar -xf - -C /out"],
                         timeout=95)
    if result.returncode or not (context / "repository/org/springframework/boot").is_dir():
        raise ValueError("isolated_maven_dependency_cache_unavailable")
    (context / "Dockerfile").write_text(f"FROM {MAVEN_IMAGE}\nCOPY repository /opt/maven-repository\n"
                                       "ENV MAVEN_OPTS=\"-Dmaven.repo.local=/opt/maven-repository -Xmx256m\"\n"
                                       "ENTRYPOINT []\n")
    tool_image = host.build(context, {"image_repository": MAVEN_CACHE_REPOSITORY}, "fixture-toolchain")["image"]
    inspected = host.invoke(["image", "inspect", "eclipse-temurin:21-jre"], timeout=20)
    source = json.loads(inspected.stdout)[0] if inspected.returncode == 0 else {}
    runtimes = [name for name in source.get("RepoDigests", []) if re.fullmatch(r"eclipse-temurin@sha256:[a-f0-9]{64}", name)]
    if len(runtimes) != 1:
        raise ValueError("isolated_java_runtime_manifest_unavailable")
    (trial.fixture / "Dockerfile").write_text(
        f"FROM {MAVEN_IMAGE} AS build\nCOPY --from=maven_cache repository /opt/maven-repository\n"
        "ENV MAVEN_OPTS=\"-Dmaven.repo.local=/opt/maven-repository -Xmx256m\"\n"
        "WORKDIR /src\nCOPY pom.xml .\nCOPY src ./src\n"
        "RUN mvn -B -o -DskipTests package\n"
        f"FROM {runtimes[0]}\nWORKDIR /app\nCOPY --from=build /src/target/app.jar /app/app.jar\n"
        'ENTRYPOINT ["java", "-jar", "/app/app.jar"]\n'
    )
    verifier = workspace / "java-verifier"
    verifier.mkdir()
    java = verifier / "OrderProbe.java"
    java.write_text(ORDER_PROBE_SOURCE)
    subprocess.run([str(trial.java_home / "bin/javac"), "--release", "21", str(java)],
                   check=True, capture_output=True, timeout=30)
    host.config["test_images"]["java"] = tool_image
    host.order_verifier = verifier
    host.order_build_context = context


def verify_order_image(host, image, *, dual=False):
    run = host.invoke(["run", "--rm", "--pull=never", "--network=none", "--memory=384m", "--cpus=1",
                       "--pids-limit=96", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only",
                       "--tmpfs", "/tmp:rw,nosuid,size=48m",
                       "--mount", f"type=bind,src={host.order_verifier},dst=/verify,readonly",
                       "--entrypoint", "java", image, "-Xmx64m", "--add-modules", "jdk.httpserver", "-cp", "/verify",
                       "OrderProbe", *(["dual"] if dual else [])], timeout=100)
    pairs, runtime, failure = {}, None, None
    for line in run.stdout.splitlines():
        if line.startswith("LP_ORDER_HTTP "):
            path, code, text = line.removeprefix("LP_ORDER_HTTP ").split()
            pairs.setdefault(path, []).append((int(code), json.loads(base64.b64decode(text, validate=True))))
        if line.startswith("LP_ORDER_RUNTIME "):
            runtime = line.removeprefix("LP_ORDER_RUNTIME ")
        if re.fullmatch(r"LP_ORDER_FAILURE (startup|runtime_configuration|business) [A-Za-z]+", line):
            failure = line.removeprefix("LP_ORDER_FAILURE ")
    status = pairs.pop("/internal/chaos/v1/app-faults", [])
    if (run.returncode or not runtime or len(status) != 1 or status[0][0] != 200
            or len(pairs) != (8 if dual else 4) or any(len(rows) != 2 for rows in pairs.values())):
        host.last_probe_failure = {"exit_code": run.returncode, "paths_observed": sorted(pairs),
                                   "java_runtime": runtime, "probe_failure": failure}
        raise ValueError("isolated_release_probe_failed")
    samples = []
    for index in range(2):
        if dual:
            def request(path, payload=None):
                if payload is not None:
                    return pairs["create" if payload["item"] else "invalid"][index]
                if path in pairs:
                    return pairs[path][index]
                return pairs["seed" if path == "/api/orders/1" else "stored"][index]
            samples.append(dual_order_probe(request))
        else:
            samples.append(order_probe(lambda path: pairs[path][index]))
    return {"samples": samples, "runtime_faults": status[0][1].get("active"), "java_runtime": runtime}
