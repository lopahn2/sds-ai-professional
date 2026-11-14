package io.liveprobe.target.inventory;

import java.time.Instant;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;

/**
 * One active fault per inventory process (contract §1.5). The v1 API and the legacy /internal/chaos/inject route
 * both claim this gate, so each gets 409 while the other is active.
 */
public class ChaosFaultGate {
    public static final String V1 = "v1";
    public static final String LEGACY = "legacy";
    private static final DateTimeFormatter RFC3339_MS =
            DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'").withZone(ZoneOffset.UTC);

    public record Active(String owner, String mode, Instant expiresAt) {
        public String expiresAtText() {
            return expiresAt == null ? "" : format(expiresAt);
        }
    }

    private Active active;

    public static String format(Instant instant) {
        return RFC3339_MS.format(instant);
    }

    public synchronized Active current() {
        return active;
    }

    /** Claims the gate for owner; returns null on success, or the conflicting fault held by the OTHER owner. */
    public synchronized Active claim(String owner, String mode, Instant expiresAt) {
        if (active != null && !active.owner().equals(owner)) return active;
        active = new Active(owner, mode, expiresAt);
        return null;
    }

    /** Returns the conflicting fault held by another owner, without claiming. */
    public synchronized Active conflict(String owner) {
        return active != null && !active.owner().equals(owner) ? active : null;
    }

    public synchronized void release(String owner) {
        if (active != null && active.owner().equals(owner)) active = null;
    }
}
