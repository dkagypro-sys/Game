const QUESTIONS=[
["Naan romba happy-ah irukkumbodhu enna panna chance adhigam?",["Romba pesuven 🗣️","Silent-ah iruppen 🤫","Songs kekka start pannuven 🎵","Thoongiduven 😴"],"Naan happy-ah irukkumbodhu en usual reaction-a yosichu paaru 😏"],
["Naan stress-la irukkumbodhu enna pannuvaen?",["Yaar kittayum pesa maaten 🤐","Unkitta pesuven ❤️","Games aaditu distract aagiduven 🎮","Thoongiduven 😴"],"Stress vandha udane naan usually yaarai theduven-nu yosichu paaru."],
["Namma rendu perum serndhu free time kidaicha naan edha choose pannuven?",["Movie 🎬","Long drive 🚗","Food date 🍕","Game night 🎮"],"Namma already enjoy pannina moments-a remember pannu."],
["Enakku surprise kudutha naan first enna react pannuven?",["Shock 😳","Sirippu 😂","Emotional 🥹","'Idhu enna da?' 😭"],"En reaction-a nee neraya times paathiruppa."],
["Naan choose panna vendiya one food-na edhu?",["Biryani 🍗","Pizza 🍕","Burger 🍔","Parotta 🥘"],"Naan repeat-ah order panra food-a think pannu."],
["Naan romba tired-ah irundha enakku edhu best?",["Pesuradhu ❤️","Sleep 😴","Tea/coffee ☕","Outside pogaradhu 🚗"],"Tired days-la naan enna prefer pannuren-nu yosichu paaru."],
["Naan oru place choose panna sonna, edhu enakku pidikkum?",["Beach 🌊","Hill station ⛰️","City night 🌃","Home 🏠"],"Namma serndhu poganum-nu pesina places-a remember pannu."],
["Naan upset-ah irundha nee enna panna enakku pidikkum?",["Advice kudukkanum 🧠","Silent-ah irundhu company kudukkanum 🤝","Comedy pannanum 😂","Space kudukkanum 🌙"],"Naan upset-ah irukkumbodhu en behaviour-a remember pannu."],
["Naan gift choose panna sonna, edhu enakku special-ah feel aagum?",["Handwritten note 💌","Expensive gift 🎁","Favourite food 🍕","Photo/memory frame 📸"],"Price vida emotion important-aa illaya-nu think pannu."],
["Namma relationship-la enakku romba important-a irukkradhu edhu?",["Trust 🤝","Time together ⏰","Understanding 🫶","All of these ❤️"],"Naan repeated-ah solli irukkura relationship value-a yosichu paaru."]
];

const $=id=>document.getElementById(id);
let peer=null, conn=null, isHost=false, myName="", partnerName="", room="";
let state={q:0,target:0,answers:[null,null],guesses:[null,null],scores:[0,0],clueUsed:[false,false]};
let selected=null;

function show(id){document.querySelectorAll(".screen").forEach(x=>x.classList.remove("active"));$(id).classList.add("active")}
function setStatus(t){$("status").textContent=t}
function send(msg){if(conn&&conn.open)conn.send(msg)}
function randomId(){return Math.random().toString(36).slice(2,8).toUpperCase()}

$("createBtn").onclick=()=>{
  myName=$("name").value.trim()||"You";
  isHost=true; room=randomId();
  peer=new Peer(room,{debug:0});
  peer.on("open",id=>{room=id.toUpperCase();$("roomCode").textContent=room;show("lobby");setStatus("Room ready")});
  peer.on("connection",c=>{conn=c;setupConn();});
  peer.on("error",e=>$("homeMsg").textContent="Room error. Try again.");
};

$("joinBtn").onclick=()=>{
  myName=$("name").value.trim()||"You";
  room=$("roomInput").value.trim().toUpperCase();
  if(room.length<4){$("homeMsg").textContent="Valid room code enter pannu ❤️";return}
  isHost=false; peer=new Peer(undefined,{debug:0});
  peer.on("open",()=>{conn=peer.connect(room,{reliable:true});setupConn()});
  peer.on("error",e=>$("homeMsg").textContent="Room kandupidikka mudila. Code check pannu.");
};

function setupConn(){
  conn.on("open",()=>{
    setStatus("Connected ❤️");
    send({type:"hello",name:myName});
    if(isHost){$("players").textContent="Partner connected ❤️";setTimeout(startGame,700)}
  });
  conn.on("data",handle);
  conn.on("close",()=>setStatus("Disconnected"));
}

function startGame(){
  state={q:0,target:0,answers:[null,null],guesses:[null,null],scores:[0,0],clueUsed:[false,false]};
  send({type:"start",partner:myName});
  renderTarget();
}

function renderTarget(){
  selected=null;
  const q=QUESTIONS[state.q];
  $("round").textContent=`Question ${state.q+1} / 10`;
  $("turn").textContent="Your turn";
  $("phase").textContent="Your real answer";
  $("question").textContent=q[0];
  $("instruction").textContent="Unmaiyaana answer illa — unakku nijama edhu correct-o adha select pannu. Partner guess pannuvanga!";
  renderOptions(q[1]);
  $("clueBtn").style.display="none";
  $("clueText").style.display="none";
  $("submitBtn").disabled=true;
  $("submitBtn").textContent="Submit my answer ❤️";
  show("game");
}

function renderGuess(){
  selected=null;
  const q=QUESTIONS[state.q];
  $("round").textContent=`Question ${state.q+1} / 10`;
  $("turn").textContent="Partner's turn to guess";
  $("phase").textContent="Guess their answer";
  $("question").textContent=q[0];
  $("instruction").textContent=`${state.target===0?myName:partnerName} enna choose panniruppanga? Guess pannu 👀`;
  renderOptions(q[1]);
  $("clueBtn").style.display="block";
  $("clueBtn").disabled=state.clueUsed[state.target];
  $("clueText").style.display="none";
  $("submitBtn").disabled=true;
  $("submitBtn").textContent="Submit guess 🔥";
  show("game");
}

function renderOptions(opts){
  $("options").innerHTML="";
  opts.forEach((x,i)=>{
    const d=document.createElement("div");d.className="option";d.textContent=String.fromCharCode(65+i)+". "+x;
    d.onclick=()=>{selected=i;document.querySelectorAll(".option").forEach(o=>o.classList.remove("selected"));d.classList.add("selected");$("submitBtn").disabled=false};
    $("options").appendChild(d);
  });
}

$("clueBtn").onclick=()=>{
  state.clueUsed[state.target]=true;
  $("clueBtn").disabled=true;
  $("clueText").textContent="💡 "+QUESTIONS[state.q][2];
  $("clueText").style.display="block";
};

$("submitBtn").onclick=()=>{
  if(selected===null)return;
  if($("phase").textContent==="Your real answer"){
    state.answers[state.target]=selected;
    send({type:"realAnswer",q:state.q,target:state.target,answer:selected});
    waitMessage("Answer saved 🔒","Partner ippo un answer-a guess pannuvanga...");
  }else{
    const correct=state.answers[state.target];
    const got=selected===correct;
    let pts=got?10:0;
    if(state.clueUsed[state.target])pts=Math.max(0,pts-2);
    state.guesses[state.target]=selected;
    state.scores[isHost?0:1]+=pts;
    send({type:"guess",q:state.q,target:state.target,guess:selected,points:pts,correct:got,clue:state.clueUsed[state.target]});
    waitMessage(got?"❤️ Correct!":"😂 Wrong guess!","Next question loading...");
  }
};

function waitMessage(a,b){$("phase").textContent=a;$("question").textContent=b;$("options").innerHTML="";$("clueBtn").style.display="none";$("submitBtn").disabled=true}

function handle(m){
  if(m.type==="hello"){
    partnerName=m.name;
    if(isHost)$("players").textContent=`${partnerName} joined ❤️`;
  }
  if(m.type==="start"){
    partnerName=m.partner;
    state={q:0,target:0,answers:[null,null],guesses:[null,null],scores:[0,0],clueUsed:[false,false]};
    // Guest waits for host to send first target instruction.
  }
  if(m.type==="realAnswer"){
    state.answers[m.target]=m.answer;
    if(isHost){
      renderGuess();
      send({type:"guessNow",q:state.q,target:state.target});
    }else{
      // Guest can only be guesser when target is host; host will tell us.
    }
  }
  if(m.type==="guessNow"){
    state.q=m.q;state.target=m.target;renderGuess();
  }
  if(m.type==="guess"){
    state.scores[isHost?1:0]+=m.points;
    showReveal(m.correct,m.points,m.guess,m.target,m.clue);
  }
  if(m.type==="next"){
    state.q=m.q;state.target=m.target;state.answers=[null,null];state.guesses=[null,null];state.clueUsed=[false,false];
    if(m.target===(isHost?0:1)) renderTarget();
    else waitMessage("Partner's turn ❤️","Avanga answer select pannitu unakku guess chance varum...");
  }
  if(m.type==="reveal"){
    showReveal(m.correct,m.points,m.guess,m.target,m.clue,true);
  }
  if(m.type==="result"){
    state.scores=m.scores;showResult();
  }
}

function showReveal(correct,pts,guess,target,clue,fromHost=false){
  const real=state.answers[target];
  $("phase").textContent=correct?"❤️ Correct!":"😂 Close, but wrong!";
  $("question").textContent=correct?`+${pts} points 🎉`:`Correct answer: ${QUESTIONS[state.q][1][real]}`;
  $("instruction").textContent=clue?"Clue use pannadhuna -2 points":"";
  $("options").innerHTML="";
  const next=document.createElement("button");next.textContent=state.q===9?"🏆 See Result":"➡️ Next Question";next.onclick=()=>{
    if(state.q===9){send({type:"finish"});showResult();return}
    const nq=state.q+1, nt=state.q%2===0?1:0;
    state.q=nq;state.target=nt;state.answers=[null,null];state.clueUsed=[false,false];
    send({type:"next",q:nq,target:nt});
    if(nt===(isHost?0:1))renderTarget();else waitMessage("Partner's turn ❤️","Avanga answer select pannitu unakku guess chance varum...");
  };$("options").appendChild(next);
  $("clueBtn").style.display="none";$("submitBtn").style.display="none";
  show("game");
  setTimeout(()=>{$("submitBtn").style.display="block"},0);
}

function showResult(){
  $("myName").textContent=myName;
  $("partnerName").textContent=partnerName||"Partner";
  const mine=state.scores[isHost?0:1], theirs=state.scores[isHost?1:0];
  $("myScore").textContent=mine;$("partnerScore").textContent=theirs;
  const pct=Math.round((mine+theirs)/2*10);
  $("percent").textContent=pct+"%";
  let title,text;
  if(pct>=90){title="Perfectly Connected ❤️";text="Semma understanding! Rendu perum oruthara oruthar nalla purinjirukkinga."}
  else if(pct>=75){title="You Know Each Other Well 💕";text="Good understanding! Innum konjam secrets discover pannunga 😏"}
  else if(pct>=55){title="Good Start 😊";text="Understanding irukku, aana innum neraya memories create pannunga!"}
  else if(pct>=30){title="Konjam Innum Pesunga 😂";text="Questions-ku answers romba different! More time together needed 😄"}
  else{title="Relationship Update Available 💀";text="Bro/Sis… innum konjam oruthara oruthar study pannanum 😂"}
  $("resultTitle").textContent=title;$("resultText").textContent=text;show("result");
}

$("againBtn").onclick=()=>{
  $("againBtn").disabled=true;
  if(isHost){
    startGame();
    send({type:"start",partner:myName});
  }else{
    send({type:"again"});
    waitMessage("Waiting ❤️","Host new game start pannitu irukkar...");
  }
  setTimeout(()=>$("againBtn").disabled=false,1000);
};
$("homeBtn").onclick=()=>location.reload();
$("copyBtn").onclick=async()=>{try{await navigator.clipboard.writeText(room);$("copyBtn").textContent="✅ Copied!"}catch(e){}};

// Guest asks host to start another game.
const oldHandle=handle;
handle=function(m){
  if(m.type==="again" && isHost){startGame();send({type:"start",partner:myName});return}
  oldHandle(m);
};

// Host needs to send first question; target alternates 0/1.
// When target is host, host answers and then host must tell guest to guess.
// When target is guest, guest answers and host guesses.
const originalSubmit=$("submitBtn").onclick;
$("submitBtn").onclick=function(){
  if(selected===null)return;
  if($("phase").textContent==="Your real answer"){
    state.answers[state.target]=selected;
    send({type:"realAnswer",q:state.q,target:state.target,answer:selected});
    if(state.target===1){
      // Guest is target: host must wait for guest's real answer.
      waitMessage("Answer saved 🔒","Partner ippo un answer-a save pannitu host guess pannuvaaru...");
    }else{
      // Host is target: guest must guess.
      send({type:"guessNow",q:state.q,target:state.target});
      waitMessage("Answer saved 🔒","Partner ippo un answer-a guess pannuvanga...");
    }
  }else{
    const correct=state.answers[state.target];
    const got=selected===correct;
    let pts=got?10:0;if(state.clueUsed[state.target])pts=Math.max(0,pts-2);
    state.guesses[state.target]=selected;
    state.scores[isHost?0:1]+=pts;
    send({type:"guess",q:state.q,target:state.target,guess:selected,points:pts,correct:got,clue:state.clueUsed[state.target]});
    showReveal(got,pts,selected,state.target,state.clueUsed[state.target]);
  }
};

// Replace message handling for answer flow where guest is target.
const baseHandle=handle;
handle=function(m){
  if(m.type==="realAnswer"){
    state.answers[m.target]=m.answer;
    if(isHost && m.target===1){
      renderGuess(); // host guesses guest
    }
    return;
  }
  if(m.type==="guessNow"){
    state.q=m.q;state.target=m.target;
    if(!isHost && m.target===0) renderGuess(); // guest guesses host
    return;
  }
  if(m.type==="guess"){
    state.scores[isHost?1:0]+=m.points;
    // Show reveal to both; guest may not know host's answer until now.
    showReveal(m.correct,m.points,m.guess,m.target,m.clue,true);
    return;
  }
  if(m.type==="start"){
    partnerName=m.partner;
    state={q:0,target:0,answers:[null,null],guesses:[null,null],scores:[0,0],clueUsed:[false,false]};
    if(!isHost) renderTarget(); // Q1 belongs to host, so guest waits
    return;
  }
  if(m.type==="next"){
    state.q=m.q;state.target=m.target;state.answers=[null,null];state.clueUsed=[false,false];
    if(state.target===(isHost?0:1)) renderTarget();
    else waitMessage("Partner's turn ❤️","Avanga secret answer select pannitu irukanga...");
    return;
  }
  if(m.type==="finish"){showResult();return}
  baseHandle(m);
};

// Host's start: Q1 target = host. Guest waits.
