package io.liveprobe.target.order;

import org.springframework.core.io.ClassPathResource;
import org.springframework.core.io.Resource;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

/** Fixed UI allowlist; generic static mappings stay disabled to preserve default-profile chaos 404s. */
@RestController
public class OrderUiController {
    @GetMapping({"/", "/index.html"})
    public ResponseEntity<Resource> index() {
        return ResponseEntity.ok().contentType(MediaType.parseMediaType("text/html;charset=UTF-8"))
                .body(new ClassPathResource("static/index.html"));
    }

    @GetMapping("/app.css")
    public ResponseEntity<Resource> stylesheet() {
        return ResponseEntity.ok().contentType(MediaType.parseMediaType("text/css;charset=UTF-8"))
                .body(new ClassPathResource("static/app.css"));
    }

    @GetMapping("/app.js")
    public ResponseEntity<Resource> script() {
        return ResponseEntity.ok().contentType(MediaType.parseMediaType("text/javascript;charset=UTF-8"))
                .body(new ClassPathResource("static/app.js"));
    }
}
