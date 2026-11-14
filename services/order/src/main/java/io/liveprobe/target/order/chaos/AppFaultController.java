package io.liveprobe.target.order.chaos;
import io.liveprobe.target.order.AppFaultState;
import java.io.IOException;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.context.annotation.Profile;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;
@RestController @Profile("chaos")
@RequestMapping("/internal/chaos/v1/app-faults")
public class AppFaultController {
 private final AppFaultState state;
 private final ChaosV1Controller auth;
 public AppFaultController(AppFaultState state,ChaosV1Controller auth){this.state=state;this.auth=auth;}
 private ResponseEntity<?> gate(HttpServletRequest r){return auth.gate(r);}
 @GetMapping public ResponseEntity<?> get(HttpServletRequest r){var denied=gate(r);return denied!=null?denied:ResponseEntity.ok(state.status());}
 @PostMapping public ResponseEntity<?> post(HttpServletRequest r){
  var denied=gate(r);if(denied!=null)return denied;
  try {if(r.getContentLengthLong()>1024)return invalid();byte[] raw=r.getInputStream().readNBytes(1025);if(raw.length>1024)return invalid();return ResponseEntity.ok(state.change(AppFaultState.parse(raw)));}
  catch(IllegalArgumentException|com.fasterxml.jackson.core.JsonProcessingException e){return invalid();}
  catch(IOException e){return unavailable();}
 }
 @DeleteMapping public ResponseEntity<?> delete(HttpServletRequest r){var denied=gate(r);if(denied!=null)return denied;try{return ResponseEntity.ok(state.clear());}catch(IOException e){return unavailable();}}
 private ResponseEntity<?> invalid(){return ResponseEntity.badRequest().body(java.util.Map.of("error","invalid_request"));}
 private ResponseEntity<?> unavailable(){return ResponseEntity.status(503).body(java.util.Map.of("error","precondition_failed","reason","state_dir_unavailable"));}
}
