package io.liveprobe.target.order;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.request;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.content;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import java.io.IOException;
import java.net.URI;
import java.util.stream.Stream;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.Arguments;
import org.junit.jupiter.params.provider.CsvSource;
import org.junit.jupiter.params.provider.MethodSource;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.core.env.Environment;
import org.springframework.core.io.ClassPathResource;
import org.springframework.http.HttpMethod;
import org.springframework.test.web.servlet.MockMvc;

/** UI resources must work with the production static resource mappings still disabled. */
@SpringBootTest
@AutoConfigureMockMvc
class OrderUiControllerTest {
    @Autowired MockMvc mvc;

    @ParameterizedTest
    @CsvSource({
            "/, index.html, text/html;charset=UTF-8",
            "/index.html, index.html, text/html;charset=UTF-8",
            "/app.css, app.css, text/css;charset=UTF-8",
            "/app.js, app.js, text/javascript;charset=UTF-8"
    })
    void servesOnlyTheFixedUiResourceWithItsExactContentType(String path, String resource, String type)
            throws Exception {
        mvc.perform(get(path))
                .andExpect(status().isOk())
                .andExpect(content().contentType(type))
                .andExpect(content().bytes(resourceBytes(resource)));
    }

    @Autowired Environment environment;

    @Test
    void genericStaticMappingsRemainDisabled() {
        assertThat(environment.getProperty("spring.web.resources.add-mappings")).isEqualTo("false");
    }

    @ParameterizedTest
    @MethodSource("unmappedRequests")
    void unmappedPathsStay404ForEveryMethodEvenWithAnOperatorToken(String path, String method, boolean token)
            throws Exception {
        var req = request(HttpMethod.valueOf(method), URI.create(path));
        if (token) req.header("Authorization", "Bearer default-profile-token-must-be-ignored-0123456789");
        mvc.perform(req).andExpect(status().isNotFound());
    }

    static Stream<Arguments> unmappedRequests() {
        return Stream.of("/not-allowed", "/static/index.html", "/DESIGN.md", "/static/DESIGN.md",
                        "/application.properties", "/META-INF/MANIFEST.MF",
                        "/internal/chaos/v1/faults", "/internal/chaos/v1/reset")
                .flatMap(path -> Stream.of("GET", "POST", "DELETE")
                        .flatMap(method -> Stream.of(false, true)
                                .map(token -> Arguments.of(path, method, token))));
    }

    @ParameterizedTest
    @ValueSource(strings = {
            "/../application.properties", "/static/../application.properties",
            "/%2e%2e/application.properties", "/%2e%2e%2fapplication.properties",
            "/%252e%252e%252fapplication.properties", "/app.css/../../application.properties",
            "/app.js%2f..%2fapplication.properties", "/app.css/../DESIGN.md",
            "/%61pplication.properties", "/static%2fDESIGN.md", "/META-INF%2fMANIFEST.MF"
    })
    void traversalAndEncodedPathsCannotReadClasspathResources(String path) throws Exception {
        // URI overload preserves percent escapes instead of encoding '%' a second time.
        mvc.perform(get(URI.create(path))).andExpect(status().isNotFound());
    }

    @ParameterizedTest
    @CsvSource({"/, index.html", "/index.html, index.html", "/app.css, app.css", "/app.js, app.js"})
    void queryParametersCannotSelectADifferentClasspathResource(String path, String resource) throws Exception {
        mvc.perform(get(path).param("path", "../application.properties")
                        .param("file", "classpath:application.properties").param("resource", "static/DESIGN.md"))
                .andExpect(status().isOk())
                .andExpect(content().bytes(resourceBytes(resource)));
    }

    private static byte[] resourceBytes(String name) throws IOException {
        try (var stream = new ClassPathResource("static/" + name).getInputStream()) {
            return stream.readAllBytes();
        }
    }
}
