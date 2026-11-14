package io.liveprobe.target.order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import java.nio.file.*;
import java.util.concurrent.atomic.AtomicLong;
import static org.assertj.core.api.Assertions.*;
class AppFaultStateTest {
 @TempDir Path directory;
 @Test void persistedTtlSurvivesRecreationAndReleaseSurvivesDelete() throws Exception {
  AtomicLong clock=new AtomicLong(1000000);
  String release="{\"faults\":{\"code_bug\":{\"params\":{}}}}";
  var state=new AppFaultState(directory.toString(),release,clock::get);
  state.change(AppFaultState.parse("{\"fault\":\"latency\",\"on\":true,\"ttl_seconds\":1}".getBytes()));
  assertThat(Files.exists(directory.resolve("app-faults.json"))).isTrue();
  var restored=new AppFaultState(directory.toString(),release,clock::get);
  assertThat(restored.enabled("latency")).isTrue();clock.addAndGet(1001);
  assertThat(restored.enabled("latency")).isFalse();assertThat(Files.exists(directory.resolve("app-faults.json"))).isFalse();
  restored.clear();assertThat(restored.enabled("code_bug")).isTrue();
  assertThat(restored.status().get("release_active")).asList().containsExactly("code_bug");
 }
 @Test void restoredTtlAboveMaximumIsDiscardedAndWarned() throws Exception {
  Files.writeString(directory.resolve("app-faults.json"),"{\"not_ready\":{\"params\":{},\"expires_at\":\"1970-01-01T02:16:40Z\"}}");
  var output=new java.io.ByteArrayOutputStream();var previous=System.out;
  try {
   System.setOut(new java.io.PrintStream(output));
   var state=new AppFaultState(directory.toString(),"",()->1000000L);
   assertThat(state.enabled("not_ready")).isFalse();
   assertThat(Files.exists(directory.resolve("app-faults.json"))).isFalse();
   assertThat(output.toString()).contains("app_fault_state_invalid");
  } finally {System.setOut(previous);}
 }

 @Test void corruptedTimestampFailsClosedWithoutPartialRestoreAndWarns() throws Exception {
  for(String expiry:new String[]{"\"bad\"","null","123","true","{}","\"999999999999999999999999\""}) {
   Files.writeString(directory.resolve("app-faults.json"),"{\"not_ready\":{\"params\":{},\"expires_at\":\"1970-01-01T00:17:40Z\"},\"crash\":{\"params\":{},\"expires_at\":"+expiry+"}}");
   var output=new java.io.ByteArrayOutputStream();var previous=System.out;
   try {
    System.setOut(new java.io.PrintStream(output));
    var state=new AppFaultState(directory.toString(),"",()->1000000L);
    assertThat(state.status().get("active")).asList().isEmpty();
    assertThat(Files.exists(directory.resolve("app-faults.json"))).isFalse();
    assertThat(output.toString()).contains("app_fault_state_invalid");
   } finally {System.setOut(previous);}
  }
 }
 @Test void expiredRestartLoopStopsAndMixedMapKeepsExactMaximum() throws Exception {
  Path file=directory.resolve("app-faults.json");
  Files.writeString(file,"{\"crash\":{\"params\":{},\"expires_at\":\"1970-01-01T00:16:40Z\"},\"not_ready\":{\"params\":{},\"expires_at\":\"1970-01-01T01:16:40Z\"}}");
  var state=new AppFaultState(directory.toString(),"",()->1000000L);
  assertThat(state.status().get("active")).asList().containsExactly("not_ready");
  assertThat(AppFaultState.parse(Files.readAllBytes(file)).has("crash")).isFalse();
  for(int i=0;i<3;i++) {
   var expired=new AppFaultState(directory.toString(),"",()->4600000L);
   assertThat(expired.status().get("active")).asList().isEmpty();
   assertThat(Files.exists(file)).isFalse();
  }
 }
 @Test void validatesEveryEntryEvenExpiredAndDoesNotPartiallyRestore() throws Exception {
  for(String invalid:new String[]{"\"unknown\":{\"params\":{},\"expires_at\":\"1970-01-01T00:00:00Z\"}","\"latency\":{\"params\":{\"bad\":1},\"expires_at\":\"1970-01-01T00:00:00Z\"}"}) {
   Files.writeString(directory.resolve("app-faults.json"),"{\"not_ready\":{\"params\":{},\"expires_at\":\"1970-01-01T00:17:40Z\"},"+invalid+"}");
   var state=new AppFaultState(directory.toString(),"",()->1000000L);
   assertThat(state.status().get("active")).asList().isEmpty();assertThat(Files.exists(directory.resolve("app-faults.json"))).isFalse();
  }
 }
 @Test void failedAtomicSaveAndDeleteNeverReportSuccessOrChangeMemory() throws Exception {
  var state=new AppFaultState(directory.toString(),"",()->1000000L);
  state.change(AppFaultState.parse("{\"fault\":\"not_ready\",\"on\":true}".getBytes()));
  Path file=directory.resolve("app-faults.json");String before=Files.readString(file);
  Files.delete(file);Files.createDirectory(file);Files.writeString(file.resolve("blocker"),"block");
  assertThatThrownBy(()->state.change(AppFaultState.parse("{\"fault\":\"liveness_fail\",\"on\":true}".getBytes()))).isInstanceOf(java.io.IOException.class);
  assertThat(state.enabled("liveness_fail")).isFalse();assertThat(state.enabled("not_ready")).isTrue();
  assertThatThrownBy(state::clear).isInstanceOf(java.io.IOException.class);assertThat(state.enabled("not_ready")).isTrue();
  Files.delete(file.resolve("blocker"));Files.delete(file);Files.writeString(file,before);
  assertThat(new AppFaultState(directory.toString(),"",()->1000000L).enabled("not_ready")).isTrue();
 }
 @Test void rejectsUnknownFaultParamsMalformedJsonAndInvalidTtl() throws Exception {
  var s=new AppFaultState("","",System::currentTimeMillis);
  for(String body:new String[]{"{\"fault\":\"bad\",\"on\":true}","{\"fault\":\"latency\",\"on\":true,\"ttl_seconds\":0}","{\"fault\":\"latency\",\"on\":true,\"params\":{\"bad\":1}}","{\"fault\":\"latency\",\"on\":true,\"on\":false}","{\"fault\":\"latency\",\"on\":true} {}"})
   assertThatThrownBy(()->s.change(AppFaultState.parse(body.getBytes()))).isInstanceOf(Exception.class);
 }
}
