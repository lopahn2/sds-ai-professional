package io.liveprobe.target.order.chaos;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.Iterator;
import java.util.Map;
import java.util.Set;

import jakarta.servlet.http.HttpServletRequest;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Profile;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import com.fasterxml.jackson.core.JsonParser;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

/**
 * KAN-96 v1 fault control API (contract 1.1-1.4). Exists only in profile chaos; loopback callers only
 * (pod port-forward); Bearer CHAOS_OPERATOR_TOKEN. Never routed through an Ingress. Never logs the header,
 * the token or its hash.
 */
@RestController
@Profile("chaos")
@RequestMapping(path = "/internal/chaos/v1", produces = MediaType.APPLICATION_JSON_VALUE)
public class ChaosV1Controller {
    static final int MAX_BODY_BYTES = 1024;
    static final int MAX_AUTH_HEADER_CHARS = 4096;
    static final int MIN_TOKEN_CHARS = 32;
    private static final Map<String, Object> INVALID = Map.of("error", "invalid_request");
    private static final ObjectMapper STRICT_JSON = new ObjectMapper()
            .enable(JsonParser.Feature.STRICT_DUPLICATE_DETECTION)
            .enable(DeserializationFeature.FAIL_ON_TRAILING_TOKENS);

    private final ChaosFaultService faults;
    private final byte[] tokenHash;

    public ChaosV1Controller(ChaosFaultService faults, @Value("${CHAOS_OPERATOR_TOKEN:}") String token) {
        if (token == null || token.isBlank() || token.length() < MIN_TOKEN_CHARS) {
            // The message never contains the value.
            throw new IllegalStateException("chaos profile requires CHAOS_OPERATOR_TOKEN of at least 32 characters");
        }
        this.faults = faults;
        this.tokenHash = sha256(token);
    }

    @PostMapping("/faults")
    public ResponseEntity<?> inject(HttpServletRequest request) {
        ResponseEntity<?> denied = gate(request);
        if (denied != null) return denied;
        JsonNode body = readJson(request);
        ChaosFaultService.Fault fault = body == null ? null : parseFault(body);
        if (fault == null) {
            faults.countRejected(body != null && body.path("mode").isTextual() ? body.get("mode").asText() : null);
            return ResponseEntity.badRequest().body(INVALID);
        }
        ChaosFaultService.Result r = faults.inject(fault);
        return ResponseEntity.status(r.status()).body(r.body());
    }

    @GetMapping("/faults")
    public ResponseEntity<?> status(HttpServletRequest request) {
        ResponseEntity<?> denied = gate(request);
        if (denied != null) return denied;
        return ResponseEntity.ok(faults.status());
    }

    @PostMapping("/reset")
    public ResponseEntity<?> reset(HttpServletRequest request) {
        ResponseEntity<?> denied = gate(request);
        if (denied != null) return denied;
        byte[] raw = readBody(request);
        if (raw == null) return ResponseEntity.badRequest().body(INVALID);
        String text = new String(raw, StandardCharsets.UTF_8).trim();
        if (!text.isEmpty()) {
            JsonNode n = parse(raw);
            if (n == null || !n.isObject() || n.size() != 0) return ResponseEntity.badRequest().body(INVALID);
        }
        ChaosFaultService.Result r = faults.reset();
        return ResponseEntity.status(r.status()).body(r.body());
    }

    // ------------------------------------------------------------------ gate: loopback (404) then auth (401)

    ResponseEntity<?> gate(HttpServletRequest request) {
        if (!isLoopback(request.getRemoteAddr())) return ResponseEntity.status(HttpStatus.NOT_FOUND).build();
        if (!authorized(request.getHeader("Authorization"))) {
            return ResponseEntity.status(HttpStatus.UNAUTHORIZED).build();
        }
        return null;
    }

    static boolean isLoopback(String remoteAddr) {
        if (remoteAddr == null) return false;
        String a = remoteAddr.trim();
        if ("::1".equals(a) || "[::1]".equals(a)) return true;
        // Only 127.0.0.0/8 dotted-quad literals are admitted. Never resolve names or
        // alternate spellings (IPv4-mapped IPv6, abbreviated numeric literals, zone IDs).
        if (!a.matches("127\\.(?:[0-9]{1,3}\\.){2}[0-9]{1,3}")) return false;
        String[] octets = a.split("\\.");
        for (int i = 1; i < 4; i++) {
            if (Integer.parseInt(octets[i]) > 255) return false;
        }
        return true;
    }

    private boolean authorized(String header) {
        if (header == null || header.length() > MAX_AUTH_HEADER_CHARS || !header.startsWith("Bearer ")) return false;
        return MessageDigest.isEqual(tokenHash, sha256(header.substring("Bearer ".length())));
    }

    private static byte[] sha256(String value) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8));
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 unavailable");
        }
    }

    // ------------------------------------------------------------------ body parsing (after auth)

    /** Reads at most 1024 bytes; null when larger or unreadable. */
    private static byte[] readBody(HttpServletRequest request) {
        if (request.getContentLengthLong() > MAX_BODY_BYTES) return null;
        try (InputStream in = request.getInputStream()) {
            byte[] buf = in.readNBytes(MAX_BODY_BYTES + 1);
            return buf.length > MAX_BODY_BYTES ? null : buf;
        } catch (IOException e) {
            return null;
        }
    }

    private static JsonNode parse(byte[] raw) {
        try {
            return STRICT_JSON.readTree(raw);
        } catch (IOException e) {
            return null;
        }
    }

    private static JsonNode readJson(HttpServletRequest request) {
        byte[] raw = readBody(request);
        if (raw == null || raw.length == 0) return null;
        JsonNode n = parse(raw);
        return n != null && n.isObject() ? n : null;
    }

    /** Exactly the contract 1.4 schemas; null for anything else. */
    static ChaosFaultService.Fault parseFault(JsonNode n) {
        JsonNode modeNode = n.get("mode");
        if (modeNode == null || !modeNode.isTextual()) return null;
        String mode = modeNode.asText();
        if (!ChaosFaultService.MODES.contains(mode)) return null;
        Set<String> allowed = "latency".equals(mode)
                ? Set.of("mode", "duration_seconds", "delay_ms")
                : Set.of("mode", "duration_seconds");
        for (Iterator<String> it = n.fieldNames(); it.hasNext(); ) {
            if (!allowed.contains(it.next())) return null;
        }
        Integer d = boundedInt(n.get("duration_seconds"), 1, ChaosFaultService.MAX_DURATION_S);
        if (d == null) return null;
        int delay = 0;
        if ("latency".equals(mode)) {
            Integer l = boundedInt(n.get("delay_ms"), 1, ChaosFaultService.MAX_DELAY_MS);
            if (l == null) return null;
            delay = l;
        }
        return new ChaosFaultService.Fault(mode, d, delay);
    }

    private static Integer boundedInt(JsonNode v, int lo, int hi) {
        if (v == null || !v.isIntegralNumber() || !v.canConvertToInt()) return null;
        int i = v.intValue();
        return i >= lo && i <= hi ? i : null;
    }
}
