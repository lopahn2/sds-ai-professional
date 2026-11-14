'use strict';
const LIMITS=[0.05,0.1,0.25,0.5,1,2.5,5,Infinity];
function escapeLabel(value) { return String(value).replace(/\\/g,'\\\\').replace(/\n/g,'\\n').replace(/"/g,'\\"'); }
function labels(values) { return '{'+Object.entries(values).map(([key,value])=>`${key}="${escapeLabel(value)}"`).join(',')+'}'; }
function createMetrics(info) {
  const requests=new Map(), errors=new Map([['TypeError',0]]);
  return {
    observe(method,uri,status,seconds) {
      const tags={method,uri,status:String(status)}, key=JSON.stringify(tags);
      if(!requests.has(key)) requests.set(key,{tags,count:0,sum:0,buckets:LIMITS.map(()=>0)});
      const record=requests.get(key); record.count++; record.sum+=seconds;
      LIMITS.forEach((limit,i)=>{if(seconds<=limit) record.buckets[i]++;});
    },
    error(name) { errors.set(name,(errors.get(name)||0)+1); },
    render(extra,allocatedBytes) {
      const lines=['# HELP http_server_requests_seconds HTTP request duration in seconds.','# TYPE http_server_requests_seconds histogram'];
      for(const record of requests.values()) {
        lines.push(`http_server_requests_seconds_count${labels(record.tags)} ${record.count}`);
        lines.push(`http_server_requests_seconds_sum${labels(record.tags)} ${record.sum}`);
        LIMITS.forEach((limit,i)=>lines.push(`http_server_requests_seconds_bucket${labels({...record.tags,le:limit===Infinity?'+Inf':limit})} ${record.buckets[i]}`));
      }
      lines.push('# TYPE app_info gauge',`app_info${labels({system:info.system,service:info.service,language:'node',version:info.version})} 1`,'# TYPE app_errors_total counter');
      for(const [exception,count] of errors) lines.push(`app_errors_total${labels({exception})} ${count}`);
      lines.push('# TYPE chaos_memory_bytes gauge',`chaos_memory_bytes ${allocatedBytes}`,'# TYPE process_resident_memory_bytes gauge',`process_resident_memory_bytes ${process.memoryUsage().rss}`);
      return lines.join('\n')+'\n'+extra;
    },
  };
}
module.exports={createMetrics};
