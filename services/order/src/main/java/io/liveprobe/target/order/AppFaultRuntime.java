package io.liveprobe.target.order;
import java.util.*;
import java.util.concurrent.*;
import jakarta.annotation.PreDestroy;
import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Component;
import org.springframework.context.event.EventListener;
import org.springframework.boot.context.event.ApplicationReadyEvent;
@Component @Profile("chaos")
public class AppFaultRuntime {
 private final AppFaultState state;
 private final AppFaultBusiness business;
 private final Semaphore slots=new Semaphore(8,true);
 private boolean held;
 private volatile boolean closed;
 private volatile double cpuSink;
 private final List<Thread> cpu=new ArrayList<>();
 private long lastJob, crashSince, lastAllocation;
 private final List<byte[]> leaked=new ArrayList<>();
 private final ScheduledExecutorService timer=Executors.newSingleThreadScheduledExecutor(r->{Thread t=new Thread(r,"app-fault-timer-order");t.setDaemon(true);return t;});
 public AppFaultRuntime(AppFaultState state,AppFaultBusiness business){this.state=state;this.business=business;}
 @jakarta.annotation.PostConstruct public void validateStartup(){if(state.enabled("startup_config_error")){try{validateStartupConfiguration();}catch(IllegalStateException e){business.error(e);Runtime.getRuntime().halt(1);}}}
 private void validateStartupConfiguration(){throw new IllegalStateException("release/runtime startup configuration validation failed");}
 @EventListener(ApplicationReadyEvent.class) public void start(){timer.scheduleWithFixedDelay(this::tick,0,100,TimeUnit.MILLISECONDS);}
 private void tick(){
  state.expire();updateWorkers();updateCpu();
  if(state.enabled("startup_config_error")){try{validateStartupConfiguration();}catch(IllegalStateException e){business.error(e);Runtime.getRuntime().halt(1);}}
  if(state.enabled("crash")){if(crashSince==0)crashSince=System.nanoTime();if(System.nanoTime()-crashSince>=3_000_000_000L)Runtime.getRuntime().halt(1);}else crashSince=0;
  if(state.enabled("memory_leak")){long ms=System.currentTimeMillis();if(ms-lastAllocation>=1000){lastAllocation=ms;int count=(int)state.number("memory_leak","mib_per_second",8);for(int i=0;i<count;i++)leaked.add(new byte[1024*1024]);}}else {leaked.clear();lastAllocation=0;}

  long now=System.nanoTime();
  if(state.enabled("background_job_failure")){if(now-lastJob>state.number("background_job_failure","interval_seconds",3)*1_000_000_000){lastJob=now;try{expirePendingOrders();}catch(OrderExpiryJobException e){business.error(e);}}}else lastJob=0;
 }
 private synchronized void updateWorkers(){boolean on=state.enabled("worker_exhaustion");if(on&&!held)held=slots.tryAcquire(8);else if(!on&&held){held=false;slots.release(8);}}
 public boolean acquire()throws InterruptedException{updateWorkers();return slots.tryAcquire(10,TimeUnit.SECONDS);}
 public void release(){slots.release();}
 private synchronized void updateCpu(){cpu.removeIf(t->!t.isAlive());int target=state.enabled("cpu_spin")?(int)state.number("cpu_spin","threads",2):0;while(cpu.size()<target){Thread t=new Thread(()->{double x=0;while(!closed&&state.enabled("cpu_spin")){for(int i=0;i<10000;i++)x+=Math.sqrt(i+1);cpuSink=x;}},"app-fault-cpu-order-"+cpu.size());t.setDaemon(true);cpu.add(t);t.start();}}
 private void expirePendingOrders(){throw new OrderExpiryJobException("order expiry failed while scanning pending records");}
 public void checkInvariant(){try{int stock=1;int reserved=2;if(reserved>stock)throw new OversellException("reserved quantity exceeds available stock");}catch(OversellException e){business.error(e);}}
 public static class OrderExpiryJobException extends RuntimeException{OrderExpiryJobException(String m){super(m);}}
 public static class OversellException extends RuntimeException{OversellException(String m){super(m);}}
 public static class WorkerPoolExhausted extends RuntimeException{WorkerPoolExhausted(){super("all 8 application slots held for 10 seconds");}}
 public static class IntermittentException extends RuntimeException{IntermittentException(){super("intermittent business processing failure");}}
 public static class DependencyError extends RuntimeException{DependencyError(Throwable cause){super("dependency connection failed",cause);}}
 @PreDestroy public void close(){closed=true;timer.shutdownNow();synchronized(this){if(held){held=false;slots.release(8);}}for(Thread t:cpu)t.interrupt();}
}
