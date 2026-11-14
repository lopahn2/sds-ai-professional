package io.liveprobe.target.order.chaos;

import java.io.IOException;
import java.nio.file.FileSystems;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.attribute.PosixFilePermissions;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Optional;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;

/**
 * KAN-96 contract 1.6: the durable crash/oom intent at $CHAOS_STATE_DIR/intent.json.
 * Written atomically (temp file + rename) with permissions 0600. Used both by the bootstrap guard
 * (before Spring) and by the running service, so it has no Spring dependency.
 */
public final class IntentStore {
    public static final String FILE_NAME = "intent.json";
    private static final ObjectMapper JSON = new ObjectMapper();

    private final Path dir;

    public IntentStore(Path dir) {
        this.dir = dir;
    }

    public static IntentStore of(String dir) {
        return new IntentStore(dir == null || dir.isBlank() ? null : Path.of(dir));
    }

    public Path file() {
        return dir == null ? null : dir.resolve(FILE_NAME);
    }

    /** True when the state dir is configured, exists, is a directory and is writable. */
    public boolean available() {
        return dir != null && Files.isDirectory(dir) && Files.isWritable(dir);
    }

    public void write(String operationId, String mode, long createdMs, long armedUntilMs) throws IOException {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("v", 1);
        m.put("operation_id", operationId);
        m.put("mode", mode);
        m.put("created_ms", createdMs);
        m.put("armed_until_ms", armedUntilMs);
        writeRaw(JSON.writeValueAsBytes(m));
    }

    /** Rewrites the current intent with "aborted":true (the guard treats that like expired). */
    public void markAborted() throws IOException {
        Optional<JsonNode> cur = read();
        if (cur.isEmpty() || !cur.get().isObject()) return;
        ObjectNode n = (ObjectNode) cur.get();
        n.put("aborted", true);
        writeRaw(JSON.writeValueAsBytes(n));
    }

    /** Raw parse of the intent; empty when absent. Throws on unreadable/unparseable content. */
    public Optional<JsonNode> read() throws IOException {
        Path f = file();
        if (f == null || !Files.exists(f)) return Optional.empty();
        return Optional.of(JSON.readTree(Files.readAllBytes(f)));
    }

    public boolean exists() {
        Path f = file();
        return f != null && Files.exists(f);
    }

    /** Deletes the intent; true when a file was actually removed. */
    public boolean delete() {
        Path f = file();
        if (f == null) return false;
        try {
            return Files.deleteIfExists(f);
        } catch (IOException e) {
            return false;
        }
    }

    private void writeRaw(byte[] content) throws IOException {
        if (dir == null) throw new IOException("state dir not configured");
        Path tmp;
        if (FileSystems.getDefault().supportedFileAttributeViews().contains("posix")) {
            tmp = Files.createTempFile(dir, "intent-", ".tmp",
                    PosixFilePermissions.asFileAttribute(PosixFilePermissions.fromString("rw-------")));
        } else {
            tmp = Files.createTempFile(dir, "intent-", ".tmp");
        }
        try {
            Files.write(tmp, content);
            Files.move(tmp, file(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
        } finally {
            Files.deleteIfExists(tmp);
        }
    }
}
