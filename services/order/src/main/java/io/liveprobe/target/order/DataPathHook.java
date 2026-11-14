package io.liveprobe.target.order;

/** Called at the start of every data-path request (GET /api/orders/check). Implemented only in profile chaos. */
public interface DataPathHook {
    void beforeDataPath();
}
