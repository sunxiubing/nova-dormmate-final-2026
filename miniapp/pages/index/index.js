function getCurrentTime(){let n=new Date();return `${n.getFullYear()}-${String(n.getMonth()+1).padStart(2,'0')}-${String(n.getDate()).padStart(2,'0')} ${String(n.getHours()).padStart(2,'0')}:${String(n.getMinutes()).padStart(2,'0')}:${String(n.getSeconds()).padStart(2,'0')}`;}

/* 状态判定。偏冷/偏热和偏湿是可以同时成立的（12℃ 且 80% 既是偏冷也是偏湿），
   两项都异常就用 + 拼起来，和 dashboard 的 computeStatus、3d 发布面板同一套规则、
   同一个输出格式（偏冷+偏湿 / 偏热+偏湿）。
   配色只取一色、温度优先，对应 web 的 overallState 优先级 —— 一个标签只能有一个底色，
   两种颜色拼不到一起，所以「偏冷+偏湿」的标签显示的是冷色。
   抽成函数是因为这段原来在 handleAnalyze 和 fetchHistoryData 里各写了一份，
   而且已经漂移过：一处阈值 t<18、一处 t<10，同一条 15℃ 记录在两个地方显示不同结论。 */
function computeStatus(t,h){
  let parts=[];
  if(t<18)parts.push('偏冷');else if(t>=30)parts.push('偏热');
  if(h>=75)parts.push('偏湿');
  return {text:parts.length?parts.join('+'):'正常',
          cls:t<18?'cold':(t>=30?'hot':(h>=75?'wet':'normal'))};
}

/* 服务端地址候选，按顺序试，第一个连上的用。
   开发者工具里 localhost 和这台电脑是同一个，永远连得通；真机上 localhost 指向手机自己，
   会快速失败，于是自动退到局域网 IP。
   局域网 IP 别当成不变的常量 —— 它由路由器分配，这台机器上一次是 192.168.131.200，
   现在已经是 192.168.171.200 了。换了网络环境，改下面这一行就行。 */
const API_HOSTS = ['http://localhost:5000', 'http://192.168.171.200:5000'];
const API_PATH = '/api/getHistory';

/* 「清空」的清零点：这个时刻之前的记录不再上屏，下拉刷新也不会自己回来，
   下一条从 #1 重新数。存本地缓存而不是只放内存 —— 只放内存的话，
   退出小程序再进来，旧记录又会被同步当成新数据全灌回来。
   注意后端一条都没删：CSV 里的老数据完整保留，网页端、分析脚本照样看得到，
   这里只是页面把这批记录划到了线外。判定语义与 web/script.js 的 clearedBefore 一致 */
const CLEAR_KEY='dormmate.clearBefore';
function getClearedBefore(){
  try{return wx.getStorageSync(CLEAR_KEY)||'';}catch(e){return '';}
}
/* 后端行的 time 列在这三个字段名之间兜底，和下面 map 里的取法必须一致，
   否则会出现「过滤用的是 A 字段、显示用的是 B 字段」这种对不上的情况 */
function rowTime(item){return String((item&&(item.time||item.date||item['时间']))||'');}

Page({
  data:{
    temp:'',humi:'',
    analysisResult:{bannerText:'等待输入...',bannerClass:'banner-normal',temp:'--',humi:'--',tempStatus:'--',humiStatus:'--',tempClass:'',humiClass:'',suggestions:['本次输入未通过校验，未生成分析结果。'],time:'--'},
    historyList:[]
  },
  onLoad(){this.fetchHistoryData();},
  onPullDownRefresh(){this.fetchHistoryData(()=>wx.stopPullDownRefresh());},
  onTempInput(e){this.setData({temp:e.detail.value});},
  onHumiInput(e){this.setData({humi:e.detail.value});},

  handleAnalyze(){
    let t=parseFloat(this.data.temp),h=parseFloat(this.data.humi);
    if(isNaN(t)||isNaN(h))return wx.showToast({title:'请输入有效温湿度',icon:'none'});
    if(t<-20||t>60)return wx.showToast({title:'温度超出范围(-20~60°C)',icon:'none'});
    if(h<0||h>100)return wx.showToast({title:'湿度超出范围(0~100%)',icon:'none'});
    this.applyAnalysis(t,h);
    let st=computeStatus(t,h);
    let newRecord={id:this.data.historyList.length+1,temp:t,humi:h,time:getCurrentTime(),statusText:st.text,statusClass:st.cls};
    this.setData({historyList:[newRecord,...this.data.historyList]});
    wx.showToast({title:'分析完成',icon:'success'});
  },

  /* 判定阈值和分支顺序保持原样（t<18 偏冷 / t>=30 偏热 / h>=75 偏湿），
     这次只调整「怎么显示」，不动「怎么判断」：
       · bannerClass 从 banner-warning/danger 换成按状态取色的 banner-cold/hot/wet
       · humiClass 的 tag-warning 改成 tag-wet（web 端偏湿是紫色，原来那个橙色是第四种颜色）
       · 只偏湿、温度正常时补一条横幅文案 —— 原来这种情况下横幅会停留在
         「温湿度处于舒适区间」，而湿度其实已经偏湿了，等于在说反话
     第三加入参 timeText：从历史同步进来时要把那条记录自己的时间显示出来，
     不能一律写成「现在」。手动分析不传，走 getCurrentTime()。 */
  applyAnalysis(t,h,timeText){
    let res={bannerText:'',bannerClass:'banner-normal',tempStatus:'正常',tempClass:'tag-normal',humiStatus:'正常',humiClass:'tag-normal',suggestions:[]};
    /* 横幅上要列出的异常项。温度和湿度可以同时异常（12℃/80%、31℃/80%），
       原来这里是 if/else if 直接往 bannerText 里塞整句，谁先命中谁赢，
       湿度那半就被吞掉了 —— 现在先把异常项收进 heads，最后一起拼 */
    let heads=[];
    if(t<18){heads.push('温度偏低');res.bannerClass='banner-cold';res.tempStatus='偏冷';res.tempClass='tag-cold';res.suggestions.push('温度偏低：建议关闭门窗、开启空调制热，夜间注意保暖。');}
    else if(t>=30){heads.push('温度偏高');res.bannerClass='banner-hot';res.tempStatus='偏热';res.tempClass='tag-hot';res.suggestions.push('温度偏高:建议开启空调制冷，并保持空气流通。');}
    if(h>=75){
      heads.push('湿度偏高');res.humiStatus='偏湿';res.humiClass='tag-wet';
      //横幅底色只取一色、温度优先（和 web 的 overallState 同序），只有温度正常时才交给湿度上色
      if(res.bannerClass==='banner-normal')res.bannerClass='banner-wet';
      res.suggestions.push('湿度偏高：建议开启除湿机或空调除湿模式，注意防霉防潮。');
    }
    else{
     res.suggestions.push(' 湿度处于舒适区间，维持现状即可。')
    }
    res.bannerText=heads.length?heads.join('、')+'，建议按下方建议调整':'温湿度处于舒适区间，维持现状即可。';
    this.setData({analysisResult:Object.assign({},res,{temp:t,humi:h,time:timeText||getCurrentTime()})});
  },

  fetchHistoryData(callback){
    wx.showLoading({title:'同步中...'});

    /* 每次同步领一个序号，回调里比对：自己不是最新那次请求了就丢掉。
       清空按钮会把序号往前推一格，让正在飞的那次同步作废 —— 否则迟到的响应
       会把刚清空的历史又灌回来（表现就是「点了清空，历史还在」）。
       注意这里是「作废某一次请求」，不是把同步整个关掉：
       清空之后下拉刷新领的是新序号，照样能把数据拉回来。 */
    this.reqSeq=(this.reqSeq||0)+1;
    const seq=this.reqSeq;
    const stale=()=>seq!==this.reqSeq;

    let hostIdx=0;
    const done=()=>{wx.hideLoading();if(callback)callback();};

    /* 依次试候选地址。原来只发一个写死的地址，IP 一变就直接报「连接失败，请检查IP」，
       而且分不清是「IP 错了」「服务没起」还是「接口路径不对」——
       现在把这几种失败迹象分别报出来，下次一眼就能定位。 */
    const tryNext=()=>{
      /* 这次同步已经被清空按钮作废了，别再往下试下一个 host、再弹一次「连不上服务端」 */
      if(stale()){return done();}
      if(hostIdx>=API_HOSTS.length){
        wx.showToast({title:'连不上服务端，请确认 app.py 已启动',icon:'none'});
        return done();
      }
      const url=API_HOSTS[hostIdx++]+API_PATH;
      wx.request({
        url:url,method:'GET',
      success:(res)=>{
        /* 请求在飞的时候用户可能已经点了清空。这时候把数据 setData 回去，
           表现就是「清空按钮没反应、历史还在」—— 清空要能真清空，迟到的响应直接丢。
           onLoad 那次同步最容易踩到：第一个 host 连不上要等超时才换下一个，
           用户等这几秒多半已经点了清空 */
        if(stale()){return done();}
        // 地址通了但拿不到数据，分两种：
        //   非 200 —— 最常见的是跑成了 server.py。它只有 /api/get_data，没有 /api/getHistory；
        //            而且两个服务都占 5000 端口，同一时间只能跑一个
        //   非数组 —— 接口对了但返回结构不是预期的
        if(res.statusCode!==200){
          console.warn('接口返回非 200：',url,res.statusCode,res.data);
          wx.showToast({title:'服务端返回 '+res.statusCode+'，请确认跑的是 app.py',icon:'none'});
          return done();
        }
        if(!Array.isArray(res.data)){
          console.warn('返回的不是数组：',url,res.data);
          wx.showToast({title:'返回数据格式不对',icon:'none'});
          return done();
        }

        console.log("web返回原始数据", res.data, url)
        /* 清零点之前的行直接不进列表。这一道和上面那个序号守卫是两件事：
           序号守卫挡的是「正在飞的这一次请求」，管不到下次刷新——
           用户清空后再下拉一次，照样是全量数据灌回来。
           想彻底不回来，就得把「清到哪一刻」记住并按它过滤 */
        const clearedBefore=getClearedBefore();
        const rows=clearedBefore?res.data.filter(item=>rowTime(item)>clearedBefore):res.data;
        const list=rows.map((item,idx)=>{
          let t=parseFloat(item.temperature||item.temp||item['温度']||0)||0;
          let h=parseFloat(item.humidity||item.humi||item['湿度']||0)||0;
          //判定和上面那张卡片共用 computeStatus，两处永远同源（原来各写一份，已经漂移过）
          let st=computeStatus(t,h);
          return {id:idx+1,temp:t,humi:h,statusText:st.text,statusClass:st.cls,
                  time:rowTime(item)};

        });
        this.setData({historyList:list});
        if (list.length > 0) {
          /* 复用同一套判定来渲染上面那张卡片：数值、标签、横幅、建议、时间全部同源，
             不会再出现「数值是这次同步来的、标签是上一次手动分析留下的」这种错配。
             原来这里写的是 currentTemp / currentHumi / currentTempTag 这几个键，
             WXML 里一个都没引用，等于白写；真正在显示的 currentStatus 又只有同步路径会写，
             所以手动点「分析」时横幅永远是空的。 */
          const latest = list[list.length - 1];
          this.applyAnalysis(latest.temp, latest.humi, latest.time);

          /* 输入框自动带上最后一次的读数，省得用户照着上面的卡片再敲一遍。
             页面刚打开时输入框是空的、卡片却已经有数了，就是这个不一致。
             只在输入框为空时才回填：下拉刷新会重跑这段，不能把用户
             正在输入、还没点分析的内容冲掉 */
          const patch={};
          if(this.data.temp==='')patch.temp=String(latest.temp);
          if(this.data.humi==='')patch.humi=String(latest.humi);
          if(Object.keys(patch).length)this.setData(patch);
        }
        done();
      },
      fail:(err)=>{
        if(stale()){return done();}
        console.warn('连不上，换下一个地址：',url,err&&err.errMsg);
        tryNext();
      }
      });
    };
    tryNext();
  },

  handleClear(){
    this.setData({temp:'',humi:'',analysisResult:{bannerText:'等待输入...',bannerClass:'banner-normal',temp:'--',humi:'--',tempStatus:'--',humiStatus:'--',tempClass:'',humiClass:'',suggestions:['本次输入未通过校验，未生成分析结果。'],time:'--'},
    historyList:[]
  });
  /* 只作废「此刻正在飞」的那次同步，不是把同步永久关掉 ——
     原来这里是 this.stopAutoLoadHistory = true，置上就再没有复位的地方，
     清空之后下拉刷新也拉不回来数据。现在改成把序号推一格：
     旧请求作废，清空之后新发起的同步照常生效 */
  this.reqSeq=(this.reqSeq||0)+1;
  /* 记下清零点，让「清空过的记录」在后续任何一次同步里都不再上屏（见 fetchHistoryData）。
     清空后新产生的记录时间晚于它，照常显示，不会连新数据一起锁死 */
  try{wx.setStorageSync(CLEAR_KEY,getCurrentTime());}catch(e){}
  },

  handleExportCSV(){
    if(this.data.historyList.length===0)return wx.showToast({title:'没有历史记录',icon:'none'});
    let csv='编号,温度,湿度,状态,时间\n';
    this.data.historyList.forEach(i=>csv+=`${i.id},${i.temp},${i.humi},${i.statusText},${i.time}\n`);
    wx.setClipboardData({data:csv,success:()=>wx.showModal({title:'导出成功',content:'历史记录已复制到剪贴板',showCancel:false})});
  }
});