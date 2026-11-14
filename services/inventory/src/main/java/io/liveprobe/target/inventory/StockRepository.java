package io.liveprobe.target.inventory;

import java.util.List;
import java.util.Optional;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

/** Read-only access to inventory.stock in the dedicated inventory database. */
@Repository
public class StockRepository {
    private final JdbcTemplate jdbc;

    public StockRepository(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    public Optional<Integer> quantity(String item) {
        List<Integer> rows = jdbc.queryForList(
                "SELECT quantity FROM inventory.stock WHERE item = ?", Integer.class, item);
        return rows.isEmpty() ? Optional.empty() : Optional.ofNullable(rows.get(0));
    }
}
