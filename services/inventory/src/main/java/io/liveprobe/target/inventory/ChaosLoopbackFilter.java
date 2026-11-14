package io.liveprobe.target.inventory;

import java.io.IOException;
import java.net.InetAddress;
import java.net.UnknownHostException;
import java.util.regex.Pattern;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.core.Ordered;
import org.springframework.web.filter.OncePerRequestFilter;

/**
 * Loopback-only guard for every /internal/chaos/** route (contract §1.1): a caller whose remote address is
 * not 127.0.0.0/8 or ::1 gets 404 with an empty body, before authentication. Operators reach the routes
 * via `kubectl port-forward pod/...`, which arrives on loopback inside the pod network namespace.
 */
public class ChaosLoopbackFilter extends OncePerRequestFilter implements Ordered {
    static final String PREFIX = "/internal/chaos";
    private static final Pattern IPV4 = Pattern.compile("^\\d{1,3}(\\.\\d{1,3}){3}$");

    @Override
    protected boolean shouldNotFilter(HttpServletRequest request) {
        String uri = request.getRequestURI();
        return uri == null || !(uri.equals(PREFIX) || uri.startsWith(PREFIX + "/"));
    }

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response, FilterChain chain)
            throws ServletException, IOException {
        if (!isLoopback(request.getRemoteAddr())) {
            response.setStatus(HttpServletResponse.SC_NOT_FOUND);
            response.setContentLength(0);
            return;
        }
        chain.doFilter(request, response);
    }

    /** Only IP literals are accepted, so no DNS lookup can ever happen here. */
    static boolean isLoopback(String remoteAddr) {
        if (remoteAddr == null || remoteAddr.isBlank()) return false;
        String addr = remoteAddr.trim();
        if (addr.startsWith("[") && addr.endsWith("]")) addr = addr.substring(1, addr.length() - 1);
        if (!IPV4.matcher(addr).matches() && !(addr.indexOf(':') >= 0 && addr.matches("^[0-9A-Fa-f:.%a-zA-Z0-9]+$"))) {
            return false;
        }
        try {
            return InetAddress.getByName(addr).isLoopbackAddress();
        } catch (UnknownHostException e) {
            return false;
        }
    }

    @Override
    public int getOrder() {
        return Ordered.HIGHEST_PRECEDENCE + 10;
    }
}
