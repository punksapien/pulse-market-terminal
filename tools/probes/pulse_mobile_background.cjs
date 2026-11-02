/* ADR-0206: finite CSS-only browser regression; fixture mode, no data requests. */
"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),path=require("node:path");
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||"playwright");
(async()=>{
  const input=process.stdin.isTTY?{}:JSON.parse(fs.readFileSync(0,"utf8")||"{}");
  const origin=input.origin;
  if(origin){
    const u=new URL(origin);
    assert(u.protocol==="https:"&&u.hostname.endsWith(".vercel.app")&&u.pathname==="/"&&!u.username&&!u.password&&!u.search&&!u.hash&&!u.port);
  }
  const browser=await chromium.launch({headless:true});
  const widths=[320,360,375,390,400,430,600,699,700,1280],results=[],errors=[];
  let dataRequests=0;
  try{
    const page=await browser.newPage({viewport:{width:390,height:740}});
    page.on("pageerror",e=>errors.push(e.name));
    await page.route("**/*",route=>{
      const u=new URL(route.request().url());
      if(u.pathname.startsWith("/api/")){dataRequests++;return route.abort();}
      return origin&&u.origin===origin?route.continue({headers:{...route.request().headers(),
        ...(input.token?{"x-vercel-protection-bypass":input.token}:{})}}):route.abort();
    });
    if(origin)await page.goto(origin+"/pulse/",{waitUntil:"domcontentloaded",timeout:30000});
    else await page.setContent(fs.readFileSync(path.resolve(__dirname,"../../pulse-preview.html"),"utf8"));
    await page.waitForFunction(()=>window.PulseDesign?.diagnostics().sample===true);
    for(const theme of ["light","dark"]){
      await page.evaluate(value=>{document.documentElement.dataset.theme=value;},theme);
      for(const width of widths){
        await page.setViewportSize({width,height:740});
        const result=await page.evaluate(()=>{
          const app=document.getElementById("pulse-app"),r=app.getBoundingClientRect();
          return {width:innerWidth,html:getComputedStyle(document.documentElement).backgroundColor,
            body:getComputedStyle(document.body).backgroundColor,surface:getComputedStyle(app).backgroundColor,
            appWidth:r.width,left:r.left,right:r.right,rootScrollWidth:document.documentElement.scrollWidth};
        });
        const expected=theme==="dark"?"rgb(31, 31, 31)":width<700?"rgb(255, 255, 255)":"rgb(212, 212, 212)";
        assert.equal(result.html,expected,JSON.stringify({theme,result}));
        assert.equal(result.body,expected,JSON.stringify({theme,result}));
        assert.equal(result.appWidth,Math.min(width,375));
        assert(Math.abs(result.left-(width-result.appWidth)/2)<1);
        assert.equal(result.rootScrollWidth,width);
        if(width<700)assert.equal(result.body,result.surface);
        results.push({theme,...result});
      }
    }
    assert.equal(dataRequests,0);assert.deepEqual(errors,[]);
    console.log(JSON.stringify({status:"pass",origin:origin||"local generated fixture",cases:results.length,
      widths,centeredLayoutPreserved:true,mobileSurfaceMatched:true,desktopCanvasPreserved:true,
      rootOverflow:false,dataRequests,pageErrors:errors,results}));
  }finally{await browser.close();}
})().catch(e=>{console.error(JSON.stringify({status:"failed",type:e.name,message:e.message.slice(0,300)}));process.exitCode=1;});
