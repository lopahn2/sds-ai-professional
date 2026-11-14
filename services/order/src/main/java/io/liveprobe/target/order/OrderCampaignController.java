package io.liveprobe.target.order;
import java.util.*;
import java.util.concurrent.atomic.AtomicLong;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;
@RestController @RequestMapping("/api/orders")
public class OrderCampaignController {
 private final ObjectProvider<AppFaultBusiness> faults;
 private final OrderController orders;
 private final AtomicLong sequence=new AtomicLong(1);
 private final Map<Long,Map<String,Object>> records=new LinkedHashMap<>();
 public OrderCampaignController(ObjectProvider<AppFaultBusiness> faults,OrderController orders){this.faults=faults;this.orders=orders;records.put(1L,Map.of("id",1,"item","a","quantity",1,"amount",100));}
 @PostMapping public synchronized ResponseEntity<?> create(@RequestBody Map<String,Object> body){
  Object raw=body.get("item");if(!(raw instanceof String item)||item.isBlank()||item.length()>64)return ResponseEntity.badRequest().body(Map.of("error","invalid_item"));
  var b=faults.getIfAvailable();if(b!=null)item=b.normalizeOrderItem(item);
  var stock=orders.check(item);if(!stock.getStatusCode().is2xxSuccessful())return stock;
  if(!Boolean.TRUE.equals(stock.getBody().get("orderable")))return ResponseEntity.status(409).body(Map.of("error","out_of_stock"));
  long id=sequence.incrementAndGet();var row=Map.<String,Object>of("id",id,"item",item,"quantity",1,"amount",100);records.put(id,row);while(records.size()>128)records.remove(records.keySet().stream().filter(key->key!=1L).findFirst().orElseThrow());return ResponseEntity.status(201).body(row);
 }
 @GetMapping("/{id}") public synchronized ResponseEntity<?> get(@PathVariable long id){var row=records.get(id);if(row==null)return ResponseEntity.notFound().build();var b=faults.getIfAvailable();if(b!=null)b.serializeOrderAmount((int)row.get("amount"));return ResponseEntity.ok(row);}
}
