package io.liveprobe.target.order.chaos;

/** Process termination hook; production uses Runtime.halt, tests inject a recorder. */
@FunctionalInterface
public interface ProcessExit {
    void halt(int status);

    ProcessExit RUNTIME_HALT = status -> {
        System.out.flush();
        System.err.flush();
        Runtime.getRuntime().halt(status);
    };
}
