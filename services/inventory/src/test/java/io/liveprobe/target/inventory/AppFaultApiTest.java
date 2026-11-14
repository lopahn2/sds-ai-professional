package io.liveprobe.target.inventory;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;
@SpringBootTest(properties={"CHAOS_OPERATOR_TOKEN=" + AppFaultApiTest.TOKEN, "spring.datasource.url=jdbc:h2:mem:appfault;DB_CLOSE_DELAY=-1", "spring.datasource.username=sa", "spring.datasource.password=", "spring.sql.init.mode=always", "spring.sql.init.schema-locations=classpath:chaos-test-schema.sql"})
@ActiveProfiles("chaos") @AutoConfigureMockMvc
class AppFaultApiTest {
 static final String TOKEN="test-app-fault-token-0123456789-abcdef";
 static final String PATH="/internal/chaos/v1/app-faults";
 @Autowired MockMvc mvc;
 @Test void cpuWorkersStopWhenFaultClears() throws Exception {
  mvc.perform(post(PATH).header("Authorization","Bearer "+TOKEN).contentType("application/json").content("{\"fault\":\"cpu_spin\",\"on\":true,\"params\":{\"threads\":2}}")).andExpect(status().isOk());
  try {Thread.sleep(250);org.assertj.core.api.Assertions.assertThat(Thread.getAllStackTraces().keySet().stream().filter(t->t.getName().startsWith("app-fault-cpu-inventory")).count()).isEqualTo(2L);}
  finally {mvc.perform(delete(PATH).header("Authorization","Bearer "+TOKEN));}
  Thread.sleep(250);org.assertj.core.api.Assertions.assertThat(Thread.getAllStackTraces().keySet().stream().filter(t->t.getName().startsWith("app-fault-cpu-inventory")).count()).isZero();
 }

 @Test void latencyHangAndSpikeDelayOnlyBusiness() throws Exception {
  for(String fault:new String[]{"latency","hang","latency_spike"}) {
   String params=fault.equals("latency_spike")?"{\"seconds\":0.12,\"ratio\":1}":"{\"seconds\":0.12}";
   mvc.perform(post(PATH).header("Authorization","Bearer "+TOKEN).contentType("application/json").content("{\"fault\":\""+fault+"\",\"on\":true,\"params\":"+params+"}")).andExpect(status().isOk());
   try {long start=System.nanoTime();mvc.perform(get("/api/stock/a")).andExpect(status().isOk());org.assertj.core.api.Assertions.assertThat((System.nanoTime()-start)/1000000).isGreaterThanOrEqualTo(100L);}
   finally {mvc.perform(delete(PATH).header("Authorization","Bearer "+TOKEN));}
  }
 }
 @Test void intermittentAndRealRefusedDependencyProduceDifferentStatuses() throws Exception {
  for(String fault:new String[]{"intermittent_exception","dependency_failure"}) {
   String params=fault.equals("intermittent_exception")?",\"params\":{\"ratio\":1}":"";
   mvc.perform(post(PATH).header("Authorization","Bearer "+TOKEN).contentType("application/json").content("{\"fault\":\""+fault+"\",\"on\":true"+params+"}")).andExpect(status().isOk());
   try {mvc.perform(get("/api/stock/a")).andExpect(status().is(fault.equals("dependency_failure")?502:500)).andExpect(jsonPath("$.error").value(fault.equals("dependency_failure")?"DependencyError":"IntermittentException"));}
   finally {mvc.perform(delete(PATH).header("Authorization","Bearer "+TOKEN));}
  }
 }
 @org.junit.jupiter.api.extension.ExtendWith(org.springframework.boot.test.system.OutputCaptureExtension.class)
 @Test void backgroundAndInvariantLogRealStacksWithoutFailingHttp(org.springframework.boot.test.system.CapturedOutput output) throws Exception {
  for(String fault:new String[]{"background_job_failure","business_invariant"}) {
   String params=fault.equals("background_job_failure")?",\"params\":{\"interval_seconds\":0.1}":"";
   mvc.perform(post(PATH).header("Authorization","Bearer "+TOKEN).contentType("application/json").content("{\"fault\":\""+fault+"\",\"on\":true"+params+"}")).andExpect(status().isOk());
   try {mvc.perform(get("/api/stock/a")).andExpect(status().isOk());Thread.sleep(350);org.assertj.core.api.Assertions.assertThat(output.getOut()).contains(fault.equals("background_job_failure")?"StockReconcileException":"OversellException").contains("AppFault");}
   finally {mvc.perform(delete(PATH).header("Authorization","Bearer "+TOKEN));}
  }
 }
 @Test void workerExhaustionWaitsAndReleasesAfterReset() throws Exception {
  mvc.perform(post(PATH).header("Authorization","Bearer "+TOKEN).contentType("application/json").content("{\"fault\":\"worker_exhaustion\",\"on\":true}")).andExpect(status().isOk());
  try {long start=System.nanoTime();mvc.perform(get("/api/stock/a")).andExpect(status().isServiceUnavailable()).andExpect(jsonPath("$.error").value("WorkerPoolExhausted"));org.assertj.core.api.Assertions.assertThat((System.nanoTime()-start)/1000000).isGreaterThanOrEqualTo(9900L);}
  finally {mvc.perform(delete(PATH).header("Authorization","Bearer "+TOKEN));}
  mvc.perform(get("/api/stock/a")).andExpect(status().isOk());
 }

 @Test void code_bugProducesRealBusinessException() throws Exception {
  mvc.perform(post(PATH).header("Authorization","Bearer "+TOKEN).contentType("application/json").content("{\"fault\":\"code_bug\",\"on\":true}" )).andExpect(status().isOk());
  try { mvc.perform(post("/api/stock/a/reserve").contentType("application/json").content("{\"quantity\":1}")).andExpect(status().isInternalServerError()).andExpect(jsonPath("$.error").value("ArithmeticException")); }
  finally {mvc.perform(delete(PATH).header("Authorization","Bearer "+TOKEN));}
 }
 @Test void code_bug_validationProducesRealBusinessException() throws Exception {
  mvc.perform(post(PATH).header("Authorization","Bearer "+TOKEN).contentType("application/json").content("{\"fault\":\"code_bug_validation\",\"on\":true}" )).andExpect(status().isOk());
  try { mvc.perform(get("/api/stock/a")).andExpect(status().isInternalServerError()).andExpect(jsonPath("$.error").value("NullPointerException")); }
  finally {mvc.perform(delete(PATH).header("Authorization","Bearer "+TOKEN));}
 }
 @Test void code_bug_serializationProducesRealBusinessException() throws Exception {
  mvc.perform(post(PATH).header("Authorization","Bearer "+TOKEN).contentType("application/json").content("{\"fault\":\"code_bug_serialization\",\"on\":true}" )).andExpect(status().isOk());
  try { mvc.perform(get("/api/stock/a/summary")).andExpect(status().isInternalServerError()).andExpect(jsonPath("$.error").value("NumberFormatException")); }
  finally {mvc.perform(delete(PATH).header("Authorization","Bearer "+TOKEN));}
 }
 @Test void errorAndProbeFaultsAreReversible() throws Exception {
  mvc.perform(post(PATH).header("Authorization","Bearer "+TOKEN).contentType("application/json").content("{\"fault\":\"error_rate\",\"on\":true,\"params\":{\"ratio\":1}}" )).andExpect(status().isOk());
  try { mvc.perform(get("/api/stock/a")).andExpect(status().isServiceUnavailable());
   mvc.perform(get(PATH).header("Authorization","Bearer "+TOKEN)).andExpect(status().isOk());
  } finally {mvc.perform(delete(PATH).header("Authorization","Bearer "+TOKEN));}
  for(String fault:new String[]{"not_ready","liveness_fail"}) {
   mvc.perform(post(PATH).header("Authorization","Bearer "+TOKEN).contentType("application/json").content("{\"fault\":\""+fault+"\",\"on\":true}")).andExpect(status().isOk());
   try {mvc.perform(get("/actuator/health/"+(fault.equals("not_ready")?"readiness":"liveness"))).andExpect(status().isServiceUnavailable());}
   finally {mvc.perform(delete(PATH).header("Authorization","Bearer "+TOKEN));}
  }
 }

 @Test void protectedApiSupportsConcurrentFaultsAndReset() throws Exception {
  mvc.perform(get(PATH).with(r->{r.setRemoteAddr("10.1.1.1");return r;})).andExpect(status().isNotFound());
  mvc.perform(get(PATH)).andExpect(status().isUnauthorized());
  mvc.perform(post(PATH).header("Authorization","Bearer "+TOKEN).contentType("application/json")
   .content("{\"fault\":\"latency\",\"on\":true,\"params\":{\"seconds\":0.01},\"ttl_seconds\":1}"))
   .andExpect(status().isOk()).andExpect(jsonPath("$.active[0]").value("latency"));
  mvc.perform(post(PATH).header("Authorization","Bearer "+TOKEN).contentType("application/json")
   .content("{\"fault\":\"code_bug\",\"on\":true}"))
   .andExpect(status().isOk()).andExpect(jsonPath("$.active.length()").value(2));
  mvc.perform(delete(PATH).header("Authorization","Bearer "+TOKEN))
   .andExpect(status().isOk()).andExpect(jsonPath("$.active.length()").value(0));
 }
}
