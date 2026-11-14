package io.liveprobe.target.order;
import java.net.URI;
import java.net.http.*;
import java.time.Duration;
import java.util.concurrent.*;
import jakarta.annotation.PreDestroy;
import org.springframework.stereotype.Component;
import org.springframework.context.annotation.Profile;
import org.springframework.context.event.EventListener;
import org.springframework.boot.context.event.ApplicationReadyEvent;
import org.springframework.core.env.Environment;
@Component @Profile("chaos")
public class AppSelfTraffic {
 private final Environment env;
 private ScheduledExecutorService timer;
 private final HttpClient client=HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build();
 private int route;
 public AppSelfTraffic(Environment env){this.env=env;}
 @EventListener(ApplicationReadyEvent.class) public void start(){
  double seconds;try{seconds=Double.parseDouble(env.getProperty("SELF_TRAFFIC_INTERVAL_SECONDS","0"));}catch(NumberFormatException e){throw new IllegalArgumentException("invalid SELF_TRAFFIC_INTERVAL_SECONDS");}
  if(!Double.isFinite(seconds)||seconds<0)throw new IllegalArgumentException("invalid SELF_TRAFFIC_INTERVAL_SECONDS");if(seconds==0)return;
  timer=Executors.newSingleThreadScheduledExecutor(r->{Thread t=new Thread(r,"app-self-traffic-order");t.setDaemon(true);return t;});timer.scheduleWithFixedDelay(this::send,0,Math.max(10,(long)(seconds*1000)),TimeUnit.MILLISECONDS);
 }
 private void send(){
  String port=env.getProperty("local.server.port",env.getProperty("server.port","8080"));
  int current=route++%3;String[] paths=new String[]{"/api/orders/check?item=a","/api/orders","/api/orders/1"};
  var request=HttpRequest.newBuilder(URI.create("http://127.0.0.1:"+port+paths[current])).timeout(Duration.ofSeconds(2));
  if(current==1)request.header("Content-Type","application/json").POST(HttpRequest.BodyPublishers.ofString("{\"item\":\"a\"}"));else request.GET();
  try{client.send(request.build(),HttpResponse.BodyHandlers.discarding());}catch(InterruptedException e){Thread.currentThread().interrupt();}catch(java.io.IOException e){System.out.println("{\"event\":\"self_traffic_failed\",\"service\":\"order\"}");}
 }
 @PreDestroy public void close(){if(timer!=null)timer.shutdownNow();client.close();}
}
