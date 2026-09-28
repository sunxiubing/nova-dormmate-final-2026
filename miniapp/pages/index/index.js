function getCurrentTime(){let n=new Date();return `${n.getFullYear()}-${String(n.getMonth()+1).padStart(2,'0')}-${String(n.getDate()).padStart(2,'0')} ${String(n.getHours()).padStart(2,'0')}:${String(n.getMinutes()).padStart(2,'0')}:${String(n.getSeconds()).padStart(2,'0')}`;}

/* 服务端地址候选，按顺序试，第一个连上的用。
   开发者工具里 localhost 和这台电脑是同一个，永远连得通；真机上 localhost 指向手机自己，
   会快速失败，于是自动退到局域网 IP。
   局域网 IP 别当成不变的常量 —— 它由路由器分配，这台机器上一次是 192.168.131.200，
   现在已经是 192.168.171.200 了。换了网络环境，改下面这一行就行。 */
const API_HOSTS = ['http://localhost:5000', 'http://192.168.171.200:5000'];
const API_PATH = '/api/getHistory';

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
    let newRecord={id:this.data.historyList.length+1,temp:t,humi:h,time:getCurrentTime(),statusText:t<10?'偏冷':(t>30?'偏热':'正常'),statusClass:t<10?'cold':(t>30?'hot':'normal')};
    this.setData({historyList:[newRecord,...this.data.historyList]});
    wx.showToast({title:'分析完成',icon:'success'});
  },

  applyAnalysis(t,h){
    let res={bannerText:'温湿度处于舒适区间，维持现状即可。',bannerClass:'banner-normal',tempStatus:'正常',tempClass:'tag-normal',humiStatus:'正常',humiClass:'tag-normal',suggestions:[]};
    if(t<18){res.bannerText='温度偏低,建议按下方便引调整';res.bannerClass='banner-warning';res.tempStatus='偏冷';res.tempClass='tag-cold';res.suggestions.push('温度偏低：建议关闭门窗、开启空调制热，夜间注意保暖。');}
    else if(t>30){res.bannerText='温度偏高，建议按下方便引调整';res.bannerClass='banner-danger';res.tempStatus='偏热';res.tempClass='tag-hot';res.suggestions.push('温度偏高:建议开启空调制冷，并保持空气流通。');}
    if(h>=75){res.humiStatus='偏湿';res.humiClass='tag-warning';res.suggestions.push('湿度偏高：建议开启除湿机或空调除湿模式，注意防霉防潮。');}
    else{
     res.suggestions.push(' 湿度处于舒适区间，维持现状即可。')
    }
    this.setData({analysisResult:Object.assign({},res,{temp:t,humi:h,time:getCurrentTime()})});
  },

  fetchHistoryData(callback){
    if(this.stopAutoLoadHistory === true){
      if(callback) callback();
      return;
    }
    wx.showLoading({title:'同步中...'});

    let hostIdx=0;
    const done=()=>{wx.hideLoading();if(callback)callback();};

    /* 依次试候选地址。原来只发一个写死的地址，IP 一变就直接报「连接失败，请检查IP」，
       而且分不清是「IP 错了」「服务没起」还是「接口路径不对」——
       现在把这几种失败迹象分别报出来，下次一眼就能定位。 */
    const tryNext=()=>{
      if(hostIdx>=API_HOSTS.length){
        wx.showToast({title:'连不上服务端，请确认 app.py 已启动',icon:'none'});
        return done();
      }
      const url=API_HOSTS[hostIdx++]+API_PATH;
      wx.request({
        url:url,method:'GET',
      success:(res)=>{
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
        const list=res.data.map((item,idx)=>{
          let t=parseFloat(item.temperature||item.temp||item['温度']||0)||0;
          let h=parseFloat(item.humidity||item.humi||item['湿度']||0)||0;
          let sText=t<10?'偏冷':(t>30?'偏热':'正常');
          let sClass=t<10?'cold':(t>30?'hot':'normal');

          let sug=[];if(t<18)sug.push("温度偏低：建议关好门窗，开启空调制热，夜间注意保暖。");else if(t>30)sug.push("温度偏高:建议开启空调制冷，并保持空气流通。");else sug.push("温度处于舒适区间，维持现状即可。");if(h>75)sug.push("湿度偏高：建议开启除湿机或空调除湿模式，注意防霉防潮。");else sug.push("湿度处于舒适区间，维持现状即可。");return {id:idx+1,temp:t,humi:h,statusText:sText,statusClass:sClass,time:item.time||item.date||item['时间']||'',suggest:sug};

        });
        this.setData({historyList:list});
        if (list.length > 0) {
          const latest = list[list.length - 1];
          this.setData({
            currentTemp: latest.temp,
            currentHumi: latest.humi,
            currentSuggest: latest.suggest,
            currentTime: latest.time,
            currentStatus: "温度" + latest.statusText,
            currentTempTag: latest.statusText,
            currentHumiTag: "正常"
          })
        }
        done();
      },
      fail:(err)=>{
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
  this.stopAutoLoadHistory = true;
  },

  handleExportCSV(){
    if(this.data.historyList.length===0)return wx.showToast({title:'没有历史记录',icon:'none'});
    let csv='编号,温度,湿度,状态,时间\n';
    this.data.historyList.forEach(i=>csv+=`${i.id},${i.temp},${i.humi},${i.statusText},${i.time}\n`);
    wx.setClipboardData({data:csv,success:()=>wx.showModal({title:'导出成功',content:'历史记录已复制到剪贴板',showCancel:false})});
  }
});