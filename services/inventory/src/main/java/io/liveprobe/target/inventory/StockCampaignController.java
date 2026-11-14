package io.liveprobe.target.inventory;
import java.util.Map;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;
@RestController @RequestMapping("/api/stock")
public class StockCampaignController {
 private final StockController stock;
 private final ObjectProvider<AppFaultBusiness> faults;
 public StockCampaignController(StockController stock,ObjectProvider<AppFaultBusiness> faults){this.stock=stock;this.faults=faults;}
 @PostMapping("/{item}/reserve") public ResponseEntity<?> reserve(@PathVariable String item,@RequestBody Map<String,Object> body){
  Object q=body.get("quantity");if(!(q instanceof Integer quantity)||quantity<1||quantity>10000)return ResponseEntity.badRequest().body(Map.of("error","invalid_quantity"));
  var b=faults.getIfAvailable();if(b!=null)quantity=b.calculateReservationQuantity(quantity);
  var available=stock.get(item);if(available.quantity()<quantity)return ResponseEntity.status(409).body(Map.of("error","insufficient_stock"));
  return ResponseEntity.ok(Map.of("item",item,"reserved",quantity,"available",available.quantity()-quantity,"simulation",true));
 }
 @GetMapping("/{item}/summary") public StockController.Stock summary(@PathVariable String item){var b=faults.getIfAvailable();if(b!=null)b.serializeStockQuantity(0);return stock.get(item);}
}
