package io.liveprobe.target.inventory;

import java.io.IOException;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.Files;
import java.nio.file.NoSuchFileException;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.attribute.PosixFilePermission;
import java.nio.file.attribute.PosixFilePermissions;
import java.util.Map;
import java.util.Set;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

/**
 * Durable crash/oom intent at $CHAOS_STATE_DIR/intent.json (contract §1.6). Written atomically
 * (temp file + rename) with permissions 0600. The emptyDir survives container restarts, not Pod deletion.
 */
public final class ChaosIntentStore {
    static final String FILE_NAME = "intent.json";
    private static final Set<PosixFilePermission> OWNER_RW = PosixFilePermissions.fromString("rw-------");
    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final Path dir;

    public ChaosIntentStore(Path dir) {
        this.dir = dir;
    }

    public Path file() {
        return dir == null ? null : dir.resolve(FILE_NAME);
    }

    /** True when the state dir is configured, exists and is writable. */
    public boolean usable() {
        return dir != null && Files.isDirectory(dir) && Files.isWritable(dir);
    }

    public void write(Map<String, Object> intent) throws IOException {
        if (!usable()) throw new IOException("state dir unavailable");
        Path tmp = Files.createTempFile(dir, ".intent-", ".tmp", PosixFilePermissions.asFileAttribute(OWNER_RW));
        try {
            Files.setPosixFilePermissions(tmp, OWNER_RW);
            Files.write(tmp, MAPPER.writeValueAsBytes(intent));
            try {
                Files.move(tmp, file(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
            } catch (AtomicMoveNotSupportedException e) {
                Files.move(tmp, file(), StandardCopyOption.REPLACE_EXISTING);
            }
        } finally {
            Files.deleteIfExists(tmp);
        }
    }

    /** Returns null when there is no intent; throws on unreadable/unparseable content. */
    public JsonNode read() throws IOException {
        if (dir == null) return null;
        try {
            byte[] bytes = Files.readAllBytes(file());
            JsonNode node = MAPPER.readTree(bytes);
            if (node == null || !node.isObject()) throw new IOException("intent is not a JSON object");
            return node;
        } catch (NoSuchFileException e) {
            return null;
        }
    }

    public boolean exists() {
        return dir != null && Files.exists(file());
    }

    /** Deletes the intent; returns true if a file was removed. Never throws. */
    public boolean delete() {
        if (dir == null) return false;
        try {
            return Files.deleteIfExists(file());
        } catch (IOException e) {
            return false;
        }
    }
}
