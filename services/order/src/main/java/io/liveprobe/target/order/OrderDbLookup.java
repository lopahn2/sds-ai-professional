package io.liveprobe.target.order;

import org.springframework.dao.DataAccessException;
import org.springframework.jdbc.core.JdbcTemplate;

/** KAN-96 contract 3: the order request-path lookup, a real pooled SELECT 1 (query timeout 1 s). */
public class OrderDbLookup {
    private final JdbcTemplate jdbc;

    public OrderDbLookup(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    public void selectOne() throws DataAccessException {
        jdbc.queryForObject("SELECT 1", Integer.class);
    }
}
