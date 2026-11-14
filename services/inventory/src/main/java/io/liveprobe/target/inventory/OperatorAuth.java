package io.liveprobe.target.inventory;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;

import jakarta.servlet.http.HttpServletRequest;

/**
 * Bearer-token check for the chaos routes (KAN-96 contract §1.2). The configured token is kept only as a
 * SHA-256 digest; presented values are hashed and compared in constant time. Nothing here is ever logged.
 */
public final class OperatorAuth {
    static final int MIN_TOKEN_LENGTH = 32;
    static final int MAX_HEADER_LENGTH = 4096;
    private static final String SCHEME = "Bearer ";

    private final byte[] tokenHash;

    public OperatorAuth(String token) {
        if (token == null || token.isBlank() || token.length() < MIN_TOKEN_LENGTH) {
            // Message deliberately never contains the (possibly partial) value.
            throw new IllegalStateException("chaos profile requires CHAOS_OPERATOR_TOKEN of at least 32 characters");
        }
        this.tokenHash = sha256(token);
    }

    public boolean authorized(HttpServletRequest request) {
        String header = request.getHeader("Authorization");
        if (header == null || header.length() > MAX_HEADER_LENGTH || !header.startsWith(SCHEME)) return false;
        return MessageDigest.isEqual(tokenHash, sha256(header.substring(SCHEME.length())));
    }

    static byte[] sha256(String value) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8));
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 unavailable", e);
        }
    }
}
