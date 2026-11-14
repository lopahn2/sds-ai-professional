package io.liveprobe.target.order;
import org.springframework.stereotype.Component;
import org.springframework.context.annotation.Profile;
import io.micrometer.core.instrument.MeterRegistry;
@Component @Profile("chaos")
public class AppFaultBusiness {
 final AppFaultState state;
 private final MeterRegistry meters;
 public AppFaultBusiness(AppFaultState state,MeterRegistry meters){this.state=state;this.meters=meters;}
 public int calculateOrderAmount(int quantity){
  // 정상 수정: 나눗셈 분모를 검증하고 정상 수량을 사용한다.
  int divisor=state.enabled("code_bug")?0:1;
  return quantity/divisor;
 }
 public String normalizeOrderItem(String item){
  // 정상 수정: 조회 결과가 없는 경우 입력 품목을 유지한다.
  String found=state.enabled("code_bug_validation")?null:item;
  return found.trim();
 }
 public int serializeOrderAmount(int quantity){
  // 정상 수정: 숫자 응답을 잘못된 지역화 문자열로 변환하지 않는다.
  return Integer.parseInt(state.enabled("code_bug_serialization")?"재고/주문 금액":Integer.toString(quantity));
 }
 public void error(Throwable e){meters.counter("app_errors","exception",e.getClass().getSimpleName()).increment();System.out.println("{\"event\":\"app_exception\",\"service\":\"order\",\"exception\":\""+e.getClass().getSimpleName()+"\"}");e.printStackTrace(System.out);}
}
