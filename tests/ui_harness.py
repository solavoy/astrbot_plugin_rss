"""Loopback-only UI smoke harness; never packaged with the Plugin View."""

from __future__ import annotations

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "views" / "rss"

BRIDGE = r"""<script>
let darkMode=false,contextHandlers=[];
let harnessFeed={public_id:41,feed_id:'internal-feed-uuid',url:'https://example.invalid/rss.xml',title:'Harness Example',enabled:true,poll_interval:300,translation_enabled:false,last_fetch_at:Date.now()/1000};
window.AstrBotPluginView={ready:async()=>({displayName:'RSS UI Harness · TEST DATA',isDark:darkMode}),
 getContext:()=>({isDark:darkMode}),onContext:handler=>{contextHandlers.push(handler);return()=>{contextHandlers=contextHandlers.filter(item=>item!==handler)}},
 apiGet:async(endpoint,params={})=>window.__harnessGet(endpoint,params),
 apiPost:async(endpoint,body={})=>window.__harnessPost(endpoint,body),
 upload:async(endpoint,file)=>endpoint==='data/import'?({feeds_imported:1,skipped_unregistered_targets:1}):({ok:true,endpoint,filename:file.name}),
 download:async(endpoint,params,name)=>({filename:name||endpoint})};
window.__harnessGet=(endpoint,params)=>{
 const page={items:[],total:0,page:Number(params.page||1),page_size:Number(params.page_size||20)};
 if(endpoint==='dashboard')return {counts:{feeds:1,active_feeds:1,feed_errors:0,lists:1,targets:1,today_success:4,failed:0,pending:1},recent_articles:[{article_id:'demo-article',feed_public_id:41,feed_title:'Harness Example',title:'Test fixture article',published_at:Date.now()/1000,canonical_url:'https://example.invalid/article'}],recent_errors:[],fetch_stats:{last_hour:3,failures:0,curl_success_rate:1,browser_attempts:0,cookie_hits:2,browser_fallbacks:0,success_rate:1},paused:false,scheduler:{paused:false,enabled:true,running:false}};
 if(endpoint==='targets')return [{target_id:'test-target',label:'Harness chat',platform:'test',platform_instance:'harness',unified_origin:'test:chat'}];
 if(endpoint==='feeds')return {items:[{...harnessFeed}],total:1,page:1,page_size:20};
 if(endpoint.startsWith('feeds/')&&Number(endpoint.split('/')[1])===Number(harnessFeed.public_id))return {feed:{...harnessFeed,description:'A clearly marked local UI test fixture.'},articles:[{article_id:'demo-article',title:'Test fixture article',canonical_url:'https://example.invalid/article',published_at:Date.now()/1000,content_blocks:[{type:'paragraph',text:'This is an isolated test fixture.'}]}],subscribers:[{target_id:'test-target',label:'Harness chat',platform:'test',platform_instance:'信息推送',unified_origin:'信息推送:GroupMessage:TEST_CHAT',enabled:true,paths:[{kind:'direct',enabled:true},{kind:'list',list_public_id:9,list_name:'Harness List',enabled:false}]}],direct_subscribers:[{target_id:'test-target',label:'Harness chat'}],list_refs:[{list_public_id:9,name:'Harness List'}],fetch_logs:[{fetched_at:Date.now()/1000,status_code:200,method:'curl'}],cookie_status:null};
 if(endpoint==='lists')return {items:[{public_id:9,list_id:'internal-list-uuid',name:'Harness List',enabled:true,poll_interval:null,member_count:1,subscriber_count:1}],total:1,page:1,page_size:20};
 if(endpoint==='lists/9')return {list:{public_id:9,list_id:'internal-list-uuid',name:'Harness List',enabled:true},members:[{feed_public_id:41,feed_id:'internal-feed-uuid',url:'https://example.invalid/rss.xml',title:'Harness Example',status:'active'}],subscribers:[{target_id:'test-target',label:'Harness chat',platform:'test',platform_instance:'信息推送',unified_origin:'信息推送:GroupMessage:TEST_CHAT',enabled:true,paths:[{kind:'list',list_public_id:9,list_name:'Harness List',enabled:true}]}],articles:[{article_id:'demo-article',feed_public_id:41,feed_title:'Harness Example',title:'Test fixture article',canonical_url:'https://example.invalid/article',published_at:Date.now()/1000,content_blocks:[{type:'paragraph',text:'This is an isolated test fixture.'}]}],fetch_logs:[{feed_public_id:41,feed_title:'Harness Example',fetched_at:Date.now()/1000,status_code:200,method:'curl'}]};
 if(endpoint==='deliveries')return {items:[{delivery_id:'demo-delivery',article_id:'demo-article',title:'Test fixture article',feed_id:'internal-feed-uuid',feed_public_id:41,feed_title:'Harness Example',target_id:'test-target',target_label:'Harness chat',platform:'test',status:'success',created_at:Date.now()/1000}],total:1,page:1,page_size:20};
 if(endpoint==='deliveries/demo-delivery')return {delivery:{delivery_id:'demo-delivery',article_id:'demo-article',title:'Test fixture article',feed_id:'demo-feed',source_path:'direct:test-target',attempts:1,retryable:false,error:null,target_id:'test-target',platform:'test',status:'success',created_at:Date.now()/1000,updated_at:Date.now()/1000},article:{title:'Test fixture article'},attempts:[{attempt_id:1,result:'success',platform_message_id:'harness-message',attempted_at:Date.now()/1000,error:null}],batches:[{batch_id:'harness-batch',card_index:0,card_count:1,status:'success',platform_message_id:'harness-message',article_ids:['demo-article'],payload:{renderer:'default',message_type:'parts',payload:{parts:[{type:'text',text:'Harness card'}]}}}]};
 if(endpoint==='data/status')return {database:{path:'TEST ONLY',size_bytes:4096,user_version:3,integrity:'ok'},counts:{feeds:1},cache:{sessions:0,media:0},scheduler:{paused:false,enabled:true,running:false},config:{test_harness:true},browser_compatibility:{status:'test fixture'},session_statuses:[]};
 return page;
};
window.__harnessPost=async(endpoint,body={})=>{
 if(endpoint==='feeds/create'){
  harnessFeed={public_id:42,feed_id:'harness-created-feed',url:body.url,title:'Harness Created Feed',enabled:true,poll_interval:null,translation_enabled:!!body.translation_enabled,last_fetch_at:null};
  return {ok:true,feed:{...harnessFeed}};
 }
 const feedUpdate=endpoint.match(/^feeds\/(\d+)\/update$/);
 if(feedUpdate&&Number(feedUpdate[1])===Number(harnessFeed.public_id)){
  if(Object.hasOwn(body,'enabled'))harnessFeed.enabled=!!body.enabled;
  if(Object.hasOwn(body,'poll_interval'))harnessFeed.poll_interval=body.poll_interval;
  if(Object.hasOwn(body,'translation_enabled'))harnessFeed.translation_enabled=!!body.translation_enabled;
  return {ok:true,feed:{...harnessFeed}};
 }
 if(endpoint.endsWith('/preview'))return {preview_only:true,article_count:1,messages:[{renderer:'default',message_type:'parts',article_ids:['demo-article'],payload:{parts:[{type:'text',text:'Harness Example\n**Test fixture article**\nThis is preview-only sample content.'},{type:'text',text:'Open original' ,url:'https://example.invalid/article'}]}}]};
 if(endpoint.endsWith('/test'))return {operation_id:'harness-operation',status:'success',target_id:'test-target'};
 if(endpoint.endsWith('/refresh'))return {status:'ok',new_articles:0};
 return {ok:true,test_harness:true,endpoint,body};
};
document.addEventListener('DOMContentLoaded',()=>{const mark=document.createElement('button');mark.textContent='TEST HARNESS · FIXTURES · NO SENDS · TOGGLE THEME';mark.style.cssText='position:fixed;right:12px;bottom:10px;z-index:9999;padding:8px 10px;border:0;border-radius:6px;background:#9b3d2e;color:white;font:11px system-ui;letter-spacing:.03em;cursor:pointer';mark.onclick=()=>{darkMode=!darkMode;for(const handler of contextHandlers)handler({isDark:darkMode})};document.body.append(mark)});
</script>"""


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DIST), **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        asset_path = urlsplit(self.path).path
        if self.path != "/" and asset_path not in {"/app.js", "/app.css"} and not asset_path.startswith("/assets/"):
            self.send_error(404)
            return
        if self.path == "/":
            html = (DIST / "index.html").read_text("utf-8").replace("</head>", BRIDGE + "</head>")
            data = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        super().do_GET()


def main():
    if not DIST.is_dir():
        raise SystemExit("Plugin View assets are missing from views/rss")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    print(f"TEST-ONLY UI harness: http://127.0.0.1:{server.server_port}/", flush=True)
    print(
        "Fixtures are in-memory. All POSTs are mocked; no real platform sends occur. Ctrl+C stops the server.",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
