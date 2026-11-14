package io.liveprobe.target.inventory;

import com.fasterxml.jackson.core.JsonParser;
import com.fasterxml.jackson.databind.*;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.context.annotation.Profile;
import org.springframework.core.env.Environment;
import org.springframework.stereotype.Component;
import java.io.IOException;
import java.nio.file.*;
import java.time.Instant;
import java.time.format.DateTimeParseException;
import java.util.*;
import java.util.function.LongSupplier;

/** Runtime TTL state and immutable image release defects. No effects occur outside profile chaos. */
@Component @Profile("chaos")
public class AppFaultState {
 public static final List<String> NAMES=List.of("code_bug","code_bug_validation","code_bug_serialization","intermittent_exception","error_rate","latency","latency_spike","hang","crash","startup_config_error","memory_leak","not_ready","liveness_fail","cpu_spin","worker_exhaustion","background_job_failure","business_invariant","dependency_failure");
 private static final ObjectMapper JSON=new ObjectMapper().enable(JsonParser.Feature.STRICT_DUPLICATE_DETECTION).enable(DeserializationFeature.FAIL_ON_TRAILING_TOKENS);
 private final Map<String,JsonNode> runtime=new LinkedHashMap<>(), release=new LinkedHashMap<>();
 private final Path file;
 private final LongSupplier clock;
 @Autowired public AppFaultState(Environment env) {this(env.getProperty("CHAOS_STATE_DIR",""),env.getProperty("RELEASE_FAULTS",""),System::currentTimeMillis);}
 public AppFaultState(String directory,String releaseJson,LongSupplier clock) {
  this.clock=clock; file=directory.isBlank()?null:Path.of(directory).resolve("app-faults.json");
  try {if(file!=null&&Files.exists(file)) {JsonNode n=JSON.readTree(Files.readAllBytes(file)); if(n==null||!n.isObject()) throw new IOException("invalid state"); n.fields().forEachRemaining(e->{validate(e.getKey(),e.getValue().path("params")); if(!e.getValue().path("expires_at").isTextual())throw new IllegalArgumentException("expiry"); if(Instant.parse(e.getValue().path("expires_at").asText()).isAfter(Instant.ofEpochMilli(clock.getAsLong()).plusSeconds(3600)))throw new IllegalArgumentException("expiry"); runtime.put(e.getKey(),e.getValue());});}}
  catch(IOException|IllegalArgumentException|DateTimeParseException e){runtime.clear();System.out.println("{\"level\":\"WARN\",\"event\":\"app_fault_state_invalid\"}");try{persist(Map.of());}catch(IOException failure){System.out.println("{\"level\":\"WARN\",\"event\":\"app_fault_state_write_failed\"}");}}
  if(!releaseJson.isBlank()) try {JsonNode n=JSON.readTree(releaseJson); if(!n.isObject()||n.size()!=1||!n.path("faults").isObject())throw new IllegalArgumentException("release"); n.path("faults").fields().forEachRemaining(e->{if(!e.getValue().isObject()||e.getValue().size()!=1||!e.getValue().has("params"))throw new IllegalArgumentException("release");validate(e.getKey(),e.getValue().get("params"));release.put(e.getKey(),e.getValue());});System.out.println(JSON.writeValueAsString(Map.of("event","release_faults_changed","active",release.keySet())));}
  catch(IOException|IllegalArgumentException e){release.clear();System.out.println("{\"event\":\"release_faults_invalid\"}");}
  expire();
 }
 public synchronized boolean enabled(String name){expire();return release.containsKey(name)||runtime.containsKey(name);}
 public synchronized double number(String name,String key,double fallback){expire();JsonNode n=release.containsKey(name)?release.get(name):runtime.get(name);return n==null?fallback:n.path("params").path(key).asDouble(fallback);}
 public synchronized Map<String,Object> change(JsonNode body) throws IOException {
  if(body==null||!body.isObject()||!body.path("fault").isTextual()||!body.path("on").isBoolean())throw new IllegalArgumentException("request");
  for(Iterator<String> it=body.fieldNames();it.hasNext();)if(!Set.of("fault","on","params","ttl_seconds").contains(it.next()))throw new IllegalArgumentException("request");
  String name=body.path("fault").asText();JsonNode p=body.has("params")?body.get("params"):JSON.createObjectNode();validate(name,p);
  JsonNode ttl=body.get("ttl_seconds");if(ttl!=null&&(!ttl.isIntegralNumber()||!ttl.canConvertToInt()||ttl.asInt()<1||ttl.asInt()>3600))throw new IllegalArgumentException("ttl");
  Map<String,JsonNode> next=new LinkedHashMap<>(runtime);
  if(body.path("on").asBoolean()) next.put(name,JSON.valueToTree(Map.of("params",p,"expires_at",Instant.ofEpochMilli(clock.getAsLong()).plusSeconds(ttl==null?900:ttl.asInt()).toString())));else next.remove(name);
  persist(next);runtime.clear();runtime.putAll(next);
  System.out.println(JSON.writeValueAsString(Map.of("event","chaos_changed","fault",name,"on",body.path("on").asBoolean(),"params",p)));return status();
 }
 public synchronized Map<String,Object> clear() throws IOException {persist(Map.of()); for(String n:runtime.keySet())System.out.println(JSON.writeValueAsString(Map.of("event","chaos_changed","fault",n,"on",false,"params",Map.of())));runtime.clear();return status();}
 public synchronized void expire(){boolean changed=runtime.entrySet().removeIf(e->!Instant.parse(e.getValue().path("expires_at").asText()).isAfter(Instant.ofEpochMilli(clock.getAsLong())));if(changed)try{persist(runtime);}catch(IOException e){System.out.println("{\"event\":\"app_fault_state_write_failed\"}");}}
 private void persist(Map<String,JsonNode> next) throws IOException {
  if(file==null)return;
  if(next.isEmpty()){Files.deleteIfExists(file);return;}
  Files.createDirectories(file.getParent());Path tmp=file.resolveSibling("app-faults.json.tmp");Files.write(tmp,JSON.writeValueAsBytes(next));
  try{Files.move(tmp,file,StandardCopyOption.ATOMIC_MOVE,StandardCopyOption.REPLACE_EXISTING);}catch(AtomicMoveNotSupportedException e){Files.move(tmp,file,StandardCopyOption.REPLACE_EXISTING);}
 }
 public synchronized Map<String,Object> status(){expire();List<Map<String,Object>> items=new ArrayList<>();List<String> active=new ArrayList<>();for(String n:NAMES){boolean r=release.containsKey(n),on=r||runtime.containsKey(n);JsonNode v=r?release.get(n):runtime.get(n);Map<String,Object> row=new LinkedHashMap<>();row.put("name",n);row.put("label",n);row.put("signal",signal(n));row.put("on",on);row.put("params",v==null?Map.of():v.path("params"));row.put("expires_at",r||v==null?null:v.path("expires_at").asText());row.put("source",r?"release":on?"runtime":null);items.add(row);if(on)active.add(n);}return Map.of("active",active,"release_active",new ArrayList<>(release.keySet()),"faults",items);}
 private static String signal(String n){return switch(n){case "not_ready"->"readiness_503";case "liveness_fail"->"liveness_503";case "crash","startup_config_error"->"exit_1";case "memory_leak"->"heap_growth";case "background_job_failure","business_invariant"->"exception_log";case "latency","latency_spike","hang"->"latency";case "cpu_spin"->"cpu";default->"5xx";};}
 public static void validate(String name,JsonNode params){
  if(!NAMES.contains(name)||params==null||!params.isObject())throw new IllegalArgumentException("fault");
  Map<String,double[]> ranges=switch(name){case "error_rate","intermittent_exception"->Map.of("ratio",new double[]{0,1});case "latency"->Map.of("seconds",new double[]{0,120});case "latency_spike"->Map.of("seconds",new double[]{0,120},"ratio",new double[]{0,1});case "hang"->Map.of("seconds",new double[]{0,120});case "memory_leak"->Map.of("mib_per_second",new double[]{1,128});case "cpu_spin"->Map.of("threads",new double[]{1,8});case "background_job_failure"->Map.of("interval_seconds",new double[]{0.1,3600});default->Map.of();};
  params.fields().forEachRemaining(e->{double[] b=ranges.get(e.getKey());JsonNode v=e.getValue();if(b==null||!v.isNumber()||!Double.isFinite(v.asDouble())||v.asDouble()<b[0]||v.asDouble()>b[1]||((e.getKey().equals("threads")||e.getKey().equals("mib_per_second"))&&!v.isIntegralNumber()))throw new IllegalArgumentException("params");});
 }
 public static JsonNode parse(byte[] raw) throws IOException{return JSON.readTree(raw);}
}
