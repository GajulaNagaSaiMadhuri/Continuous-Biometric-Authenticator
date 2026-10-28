const $ = id => document.getElementById(id);
let stream = null, timer = null, busy = false, unlocked = false;
const interval = 2000;

function msg(id, text, kind = "") {
  $(id).textContent = text || "";
  $(id).className = "msg " + kind;
}

function lockUI(locked, name = "") {
  unlocked = !locked;
  $("overlay").classList.toggle("hidden", !locked);
  $("workstatus").textContent = locked ? "Locked" : "Unlocked";
  $("workstatus").className = "pill " + (locked ? "bad" : "good");
  $("identity").textContent = locked ? "No authenticated user" : "Authenticated as " + name;
  $("logout").disabled = locked;

  if (locked) {
    $("protected").innerHTML = '<div class="empty"><b>🔐</b><h3>Your workspace is protected</h3><p>Authenticate a registered user to display protected content.</p></div>';
    stopMonitor();
  } else {
    $("protected").innerHTML = '<div class="grant"><b>✓</b><div><h3>Access granted</h3><p>Identity verified. Continuous checks are active.</p></div></div><div class="private"><h3>Protected content</h3><p>This area represents the content protected by biometric verification.</p><ul><li>Access belongs to the authenticated username.</li><li>Camera checks run approximately every two seconds.</li><li>Failed checks can automatically lock this area.</li></ul></div>';
    startMonitor();
  }
}

function capture() {
  if (!stream || $("video").readyState < 2 || !$("video").videoWidth) {
    throw Error("Start camera and wait for the preview.");
  }
  const canvas = $("canvas"), video = $("video");
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
  return canvas.toDataURL("image/jpeg", 0.8);
}

async function post(url, data) {
  const response = await fetch(url, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(data)
  });
  return {r: response, d: await response.json()};
}

async function startCamera() {
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: {facingMode: "user"}, audio: false
    });
    $("video").srcObject = stream;
    await $("video").play();
    $("placeholder").classList.add("hidden");
    $("start").disabled = true;
    $("stop").disabled = false;
    $("register").disabled = false;
    $("auth").disabled = false;
    $("camstate").textContent = "Camera running";
    $("camstate").className = "pill good";
  } catch (error) {
    msg("authmsg", "Camera error: " + error.message + ". Allow camera access.", "bad");
  }
}

function stopMonitor() {
  if (timer) clearTimeout(timer);
  timer = null;
}

function stopCamera() {
  stopMonitor();
  if (stream) stream.getTracks().forEach(track => track.stop());
  stream = null;
  $("video").srcObject = null;
  $("placeholder").classList.remove("hidden");
  $("start").disabled = false;
  $("stop").disabled = true;
  $("register").disabled = true;
  $("auth").disabled = true;
  $("camstate").textContent = "Camera off";

  if (unlocked) {
    lockUI(true);
    msg("authmsg", "Camera stopped. Workspace locked.", "bad");
  }
}

/* Captures a short sequence so the backend can check open -> closed -> open eyes. */
async function captureLiveness() {
  msg("authmsg", "Look at the camera and blink once naturally...");
  const frames = [];
  for (let i = 0; i < 12; i++) {
    frames.push(capture());
    await new Promise(resolve => setTimeout(resolve, 180));
  }
  return frames;
}

async function register() {
  try {
    const username = $("username").value.trim();
    if (!username) return msg("regmsg", "Enter a username.", "bad");

    $("register").disabled = true;
    msg("regmsg", "Please blink naturally while looking at the camera...");
    const liveness_images = await captureLiveness();
    const image = liveness_images[liveness_images.length - 1];
    const {d} = await post("/api/register", {username, image, liveness_images});
    msg("regmsg", d.message, d.ok ? "good" : "bad");
    if (d.ok) {
      refreshUsers();
      refreshLogs();
    }
  } catch (error) {
    msg("regmsg", error.message, "bad");
  } finally {
    $("register").disabled = !stream;
  }
}

async function auth() {
  try {
    $("auth").disabled = true;
    msg("authmsg", "Please blink naturally while looking at the camera...");
    const liveness_images = await captureLiveness();
    const image = liveness_images[liveness_images.length - 1];
    const {r, d} = await post("/api/authenticate", {image, liveness_images});

    if (!r.ok || !d.ok) {
      lockUI(true);
      msg("authmsg", d.message, "bad");
    } else {
      lockUI(false, d.username);
      msg("authmsg", d.message + " Similarity: " + d.score, "good");
      $("monitor").textContent = "Continuous verification active.";
    }
    refreshLogs();
  } catch (error) {
    msg("authmsg", error.message, "bad");
  } finally {
    $("auth").disabled = !stream;
  }
}

async function check() {
  if (!unlocked || !stream || busy) return;
  busy = true;
  try {
    const {r, d} = await post("/api/continuous-check", {image: capture()});
    if (!r.ok || d.locked) {
      lockUI(true);
      msg("authmsg", d.message, "bad");
      $("monitor").textContent = "Locked. Authenticate again.";
      refreshLogs();
    } else if (d.ok) {
      $("monitor").textContent = `Identity verified for ${d.username}. Similarity: ${d.score}`;
    } else {
      $("monitor").textContent = d.message;
    }
  } catch (error) {
    $("monitor").textContent = "Camera check error: " + error.message;
  } finally {
    busy = false;
    if (unlocked && stream) timer = setTimeout(check, interval);
  }
}

function startMonitor() {
  stopMonitor();
  timer = setTimeout(check, 500);
}

async function logout() {
  try { await post("/api/logout", {}); } catch (error) {}
  lockUI(true);
  msg("authmsg", "Workspace locked.", "good");
  refreshLogs();
}

function esc(value) {
  return String(value).replace(/[&<>"']/g, ch => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[ch]));
}

async function status() {
  try {
    const data = await (await fetch("/api/status")).json();
    $("connection").textContent = data.models_ready ? `Models ready · ${data.users} users` : "Model files missing";
    $("connection").className = "pill " + (data.models_ready ? "good" : "bad");
    if (!data.models_ready) msg("authmsg", data.model_error, "bad");
  } catch (error) {
    $("connection").textContent = "Server unavailable";
  }
}

async function refreshUsers() {
  try {
    const data = await (await fetch("/api/users")).json();
    $("users").innerHTML = data.users.length
      ? data.users.map(user => `<div class="user"><span class="avatar">${esc(user.username[0].toUpperCase())}</span><div><b>${esc(user.username)}</b><small>Registered ${esc(new Date(user.created_at).toLocaleString())}</small></div><span class="pill good">Template saved</span></div>`).join("")
      : "No users registered yet.";
  } catch (error) {
    $("users").textContent = "Could not load users.";
  }
}

async function refreshLogs() {
  try {
    const data = await (await fetch("/api/logs")).json();
    $("logs").innerHTML = data.logs.length
      ? data.logs.map(item => `<tr><td>${esc(new Date(item.created_at).toLocaleTimeString())}</td><td>${esc(item.username || "—")}</td><td>${esc(item.event)}</td><td>${item.score == null ? "—" : Number(item.score).toFixed(4)}</td></tr>`).join("")
      : '<tr><td colspan="4">No activity recorded yet.</td></tr>';
  } catch (error) {
    $("logs").textContent = "Could not load logs.";
  }
}

$("start").onclick = startCamera;
$("stop").onclick = stopCamera;
$("register").onclick = register;
$("auth").onclick = auth;
$("logout").onclick = logout;
$("usersrefresh").onclick = refreshUsers;
$("logsrefresh").onclick = refreshLogs;

window.addEventListener("beforeunload", () => {
  stopMonitor();
  if (stream) stream.getTracks().forEach(track => track.stop());
});

lockUI(true);
status();
refreshUsers();
refreshLogs();
setInterval(status, 10000);
