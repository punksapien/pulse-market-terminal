'use strict';
const assert=require('node:assert/strict');
const {assess}=require('../tools/probes/pulse_release_browser.cjs');
const sample={rows:350,missingPrice:0,missingVolume:0,phase:'ready',numericChanges:10,sortedDescending:true};
const transport={'price:200':1,'price:304':10,'snapshot/part:200':8,'1:200':2};
assert.equal(assess([sample],transport).availability_pass,true);
for(const change of [{rows:349},{missingPrice:1},{missingVolume:1},{phase:'stale'},
  {numericChanges:0},{numericChanges:1},{numericChanges:undefined},{sortedDescending:false}])
  assert.equal(assess([{...sample,...change}],transport).availability_pass,false,JSON.stringify(change));
for(const status of [{...transport,'snapshot/part:503':1},{...transport,'price:503':1},
  {...transport,'1:429':1},{...transport,'unsafe':1},{...transport,'price:200':0},{...transport,'1:200':NaN}])
  assert.equal(assess([sample],status).availability_pass,false);
assert.equal(assess([],transport).availability_pass,false);
assert.equal(assess(null,transport).availability_pass,false);
assert.equal(assess([sample],null).availability_pass,false);
console.log('release gate: stale phases, HTTP failures and vacuous numeric sorting cannot pass');
