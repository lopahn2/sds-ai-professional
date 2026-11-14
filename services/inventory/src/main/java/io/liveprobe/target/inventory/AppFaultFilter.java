package io.liveprobe.target.inventory;
import java.io.IOException;
import java.util.concurrent.ThreadLocalRandom;
import jakarta.servlet.*;
import jakarta.servlet.http.*;
import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;
@Component @Profile("chaos")
public class AppFaultFilter extends OncePerRequestFilter {
 private final AppFaultBusiness business;
 private final AppFaultState state;
 private final AppFaultRuntime runtime;
 public AppFaultFilter(AppFaultBusiness business,AppFaultState state,AppFaultRuntime runtime){this.business=business;this.state=state;this.runtime=runtime;}
 @Override protected boolean shouldNotFilter(HttpServletRequest r){return !r.getRequestURI().startsWith("/api/")&&!r.getRequestURI().startsWith("/actuator/health/");}
 @Override protected void doFilterInternal(HttpServletRequest r,HttpServletResponse s,FilterChain chain)throws IOException,ServletException {
  String path=r.getRequestURI();
  if(path.startsWith("/actuator/")){
   if((path.equals("/actuator/health/readiness")&&state.enabled("not_ready"))||(path.equals("/actuator/health/liveness")&&state.enabled("liveness_fail"))){reply(s,503,"DOWN");return;}
   chain.doFilter(r,s);return;
  }
  boolean acquired=false;
  try{
   acquired=runtime.acquire();if(!acquired)throw new AppFaultRuntime.WorkerPoolExhausted();
   pause("latency",2);pause("hang",40);
   if(state.enabled("latency_spike")&&ThreadLocalRandom.current().nextDouble()<state.number("latency_spike","ratio",0.3))pause("latency_spike",3);
   if(state.enabled("error_rate")&&ThreadLocalRandom.current().nextDouble()<state.number("error_rate","ratio",0.5)){reply(s,503,"injected_error");return;}
   if(state.enabled("intermittent_exception")&&ThreadLocalRandom.current().nextDouble()<state.number("intermittent_exception","ratio",0.15))throw new AppFaultRuntime.IntermittentException();
   if(state.enabled("dependency_failure")){
    try(java.net.Socket socket=new java.net.Socket()){socket.connect(new java.net.InetSocketAddress("127.0.0.1",1),2000);}
    catch(IOException e){throw new AppFaultRuntime.DependencyError(e);}
   }
   if(state.enabled("business_invariant"))runtime.checkInvariant();
   chain.doFilter(r,s);
  }catch(Exception e){if(e instanceof InterruptedException)Thread.currentThread().interrupt();Throwable root=e;while(root instanceof ServletException&&root.getCause()!=null)root=root.getCause();business.error(root);if(!s.isCommitted()){s.resetBuffer();reply(s,root instanceof AppFaultRuntime.DependencyError?502:root instanceof AppFaultRuntime.WorkerPoolExhausted?503:500,root.getClass().getSimpleName());}}
  finally {if(acquired)runtime.release();}
 }
 private void pause(String name,double fallback)throws InterruptedException {if(state.enabled(name)){long end=System.nanoTime()+(long)(state.number(name,"seconds",fallback)*1_000_000_000);while(state.enabled(name)&&System.nanoTime()<end)Thread.sleep(Math.min(50,Math.max(1,(end-System.nanoTime())/1_000_000)));}}
 static void reply(HttpServletResponse s,int status,String error)throws IOException{s.setStatus(status);s.setContentType("application/json");s.getWriter().write("{\"error\":\""+error+"\"}");}
}
