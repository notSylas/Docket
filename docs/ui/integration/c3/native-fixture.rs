// Synthetic native-review worker; no production module or library access.
use serde_json::{json,Value};
use std::{io::{self,BufRead,Write},time::{SystemTime,UNIX_EPOCH,Duration},fs::OpenOptions};
fn emit(v:Value){println!("{}",v);io::stdout().flush().unwrap();}
fn main(){
 for line in io::stdin().lock().lines(){
  let Ok(line)=line else{break};let Ok(req)=serde_json::from_str::<Value>(&line) else{continue};let id=req["id"].clone();
  let result=match req["method"].as_str().unwrap_or(""){
   "ping"=>json!({"pong":true,"protocol":1,"features":["health.v1","sources.counts.v1","ask.meta.v1"]}),
   "status"=>json!({"worker":"ok","model":"synthetic-worker","chunks":3}),
   "health"=>{let mut v:Value=serde_json::from_str(&std::fs::read_to_string("/tmp/docket-c3-review/health.json").unwrap()).unwrap();let now=SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis() as u64;v["checked_at_ms"]=json!(now);for n in ["process","database","models","indexing"]{v[n]["checked_at_ms"]=json!(now);}v},
   "sources.list"=>json!({"sources":[{"id":"synthetic-a","display_name":"Synthetic reference library","path":"/synthetic-only","status":"active","files":2,"readiness":"partial"},{"id":"synthetic-b","display_name":"Synthetic comparison notes","path":"/synthetic-only","status":"active","files":1}]}),
   "query.ask"=>{
    let mode=std::fs::read_to_string("/tmp/docket-c3-review/mode").unwrap();
    writeln!(OpenOptions::new().create(true).append(true).open("/tmp/docket-c3-review/requests.jsonl").unwrap(),"{}",json!({"mode":mode,"params":req["params"]})).unwrap();
    for phase in ["retrieving","reading","answering"]{emit(json!({"jsonrpc":"2.0","method":"progress","params":{"id":id,"phase":phase}}));}
    if mode=="error" {emit(json!({"jsonrpc":"2.0","id":id,"error":{"code":-32000,"message":"Synthetic worker error for recovery review"}}));continue;}
    if mode=="hang" {emit(json!({"jsonrpc":"2.0","method":"delta","params":{"id":id,"text":"Synthetic provisional answer to discard."}}));std::thread::sleep(Duration::from_secs(3600));}
    let answer="The synthetic reference lists a day rate of 2,650 [rates#1].\n\nThe comparison note records an earlier rate of 2,400 [notes#2]. These are review fixtures, not library findings.";
    if mode=="stream" { for text in ["The synthetic reference ","lists a day rate of 2,650 ","[rates#1]. This provisional text is not checked."] {emit(json!({"jsonrpc":"2.0","method":"delta","params":{"id":id,"text":text}}));std::thread::sleep(Duration::from_secs(3));}}
    emit(json!({"jsonrpc":"2.0","method":"progress","params":{"id":id,"phase":"validating"}}));
    if mode=="abstain"{json!({"answer":"The synthetic fixtures do not support that comparison.","abstained":true,"abstention_reason":"NO_EVIDENCE","citations":[],"scope_file_count":2,"warning_codes":["no_citations"],"timings":{"total_s":0.02}})}else{json!({"answer":answer,"abstained":false,"citations":[{"label":"[rates#1]","source_display_name":"Synthetic rates","chunk_id":"fixture-rate-1","excerpt":"Synthetic reference only. Equipment day rate: 2,650."},{"label":"[notes#2]","source_display_name":"Synthetic comparison note","chunk_id":"fixture-note-2","excerpt":"Synthetic comparison only. Previous equipment day rate: 2,400."}],"scope_file_count":2,"timings":{"total_s":0.02,"by_phase":{"retrieve":0.01,"answer":0.01}}})}
   },_=>json!({})};
  emit(json!({"jsonrpc":"2.0","id":id,"result":result}));
 }
}
