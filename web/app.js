// GroupCreativity Toolkit — browser review UI.
// Transcript text is untrusted: it is only ever written with textContent /
// .value, never innerHTML.
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const views = { login: $("view-login"), groups: $("view-groups"), review: $("view-review") };

  const state = {
    group: null,        // name of the group open in the review view
    snapshot: null,
    busy: false,
    pendingDrop: false, // an empty box was saved once; a second save drops the fragment
    heartbeat: null,
    groupsTimer: null,
  };

  // ---------------------------------------------------------------- helpers

  async function api(method, path, body) {
    const options = { method, credentials: "same-origin", headers: {} };
    if (method !== "GET") {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body || {});
    }
    let response;
    try {
      response = await fetch(path, options);
    } catch (err) {
      return { status: 0, data: { error: "network" } };
    }
    let data = {};
    try { data = await response.json(); } catch (err) { /* empty body */ }
    return { status: response.status, data };
  }

  function show(name) {
    for (const key of Object.keys(views)) views[key].hidden = key !== name;
    if (name !== "review") stopHeartbeat();
    if (name !== "groups") stopGroupsTimer();
  }

  function setMessage(el, text, kind) {
    el.textContent = text || "";
    el.className = "message" + (kind ? " " + kind : "");
  }

  function sessionExpired() {
    show("login");
    setMessage($("login-message"), "Your session ended. Please enter the passcode again.", "error");
  }

  // ------------------------------------------------------------------ login

  async function boot() {
    const { data } = await api("GET", "/api/me");
    if (data.authenticated) {
      $("whoami").textContent = data.name;
      showGroups();
    } else {
      show("login");
      $("passcode").focus();
    }
  }

  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = $("login-message");
    setMessage(message, "");
    const { status, data } = await api("POST", "/api/login", {
      passcode: $("passcode").value,
      name: $("reviewer-name").value,
    });
    if (status === 200) {
      $("whoami").textContent = data.name;
      $("passcode").value = "";
      showGroups();
    } else if (status === 429) {
      setMessage(message, "Too many wrong attempts. Try again in " + data.retry_after + " seconds.", "error");
    } else if (status === 401) {
      setMessage(message, "That passcode isn't right.", "error");
      $("passcode").select();
    } else {
      setMessage(message, "Couldn't reach the server. Check the address and try again.", "error");
    }
  });

  // ----------------------------------------------------------------- groups

  function stopGroupsTimer() {
    if (state.groupsTimer) { clearInterval(state.groupsTimer); state.groupsTimer = null; }
  }

  async function showGroups() {
    show("groups");
    await refreshGroups();
    stopGroupsTimer();
    state.groupsTimer = setInterval(refreshGroups, 8000);
  }

  async function refreshGroups() {
    const { status, data } = await api("GET", "/api/groups");
    if (status === 401) return sessionExpired();
    if (status !== 200) {
      return setMessage($("groups-message"), "Couldn't load the group list.", "error");
    }
    const list = $("group-list");
    list.replaceChildren();
    if (!data.groups.length) {
      setMessage($("groups-message"), "The host hasn't loaded any groups.", "");
      return;
    }
    setMessage($("groups-message"), "");
    for (const group of data.groups) {
      const item = document.createElement("li");

      const name = document.createElement("span");
      name.className = "group-name";
      name.textContent = group.name;

      const info = document.createElement("span");
      info.className = "group-status";
      let text = group.decided + " of " + group.idea_rows + " rows reviewed";
      if (group.status === "in_use") {
        text = "In use by " + (group.holder || "another reviewer");
        info.classList.add("in-use");
      } else if (group.status === "yours") {
        text += " · you have this open";
      }
      info.textContent = text;

      const button = document.createElement("button");
      button.type = "button";
      button.className = "primary";
      button.textContent = group.decided >= group.idea_rows && group.idea_rows > 0 ? "Review again" : "Review";
      button.disabled = group.status === "in_use" || group.idea_rows === 0;
      button.addEventListener("click", () => openGroup(group.name));

      item.append(name, info, button);
      list.append(item);
    }
  }

  async function openGroup(name) {
    const { status, data } = await api("POST", "/api/groups/" + encodeURIComponent(name) + "/open");
    if (status === 401) return sessionExpired();
    if (status === 409) {
      setMessage($("groups-message"), name + " is being reviewed by " + (data.holder || "someone else") + ".", "error");
      return refreshGroups();
    }
    if (status !== 200) {
      return setMessage($("groups-message"), "Couldn't open " + name + ".", "error");
    }
    state.group = name;
    show("review");
    setMessage($("message"), "");
    render(data.snapshot);
    startHeartbeat();
  }

  // ----------------------------------------------------------------- review

  function render(snapshot) {
    state.snapshot = snapshot;
    state.pendingDrop = false;
    $("progress").textContent = snapshot.progress_text;
    $("meta").textContent = snapshot.meta_text;
    $("original").textContent = snapshot.original_text;
    $("edit").value = snapshot.edit_text;
    $("next-meta").textContent = snapshot.next.meta;
    $("next-text").textContent = snapshot.next.text;

    applyEnabled();
    if (snapshot.done) {
      setMessage($("message"), "All rows reviewed. Your ideas were saved to " + snapshot.output_name + " on the host computer.", "ok");
    } else {
      $("edit").focus();
    }
  }

  // Buttons are disabled while a request is in flight (prevents double
  // submits) and whenever the current snapshot makes them meaningless.
  function applyEnabled() {
    const snap = state.snapshot;
    const busy = state.busy;
    const done = !snap || snap.done;
    $("edit").disabled = done;
    $("btn-save").disabled = busy || done;
    $("btn-discard").disabled = busy || done;
    $("btn-split").disabled = busy || done;
    $("btn-combine").disabled = busy || done || snap.next.kind !== "row";
    $("btn-back").disabled = busy || !snap || !snap.can_back;
  }

  async function act(action, extra) {
    if (state.busy || !state.group) return;
    state.busy = true;
    applyEnabled();
    setMessage($("message"), "");
    const body = Object.assign({ action, revision: state.snapshot.revision }, extra || {});
    const { status, data } = await api(
      "POST", "/api/groups/" + encodeURIComponent(state.group) + "/action", body);
    state.busy = false;
    applyEnabled();

    if (status === 200) {
      render(data.snapshot);
    } else if (status === 401) {
      sessionExpired();
    } else if (status === 409 && data.error === "stale") {
      render(data.snapshot);
      setMessage($("message"), "This page was out of date and has been refreshed — nothing was changed.", "error");
    } else if (status === 409) {
      leaveReview("Another reviewer now has " + state.group + ".");
    } else if (status === 400 && data.snapshot) {
      setMessage($("message"), data.message || "That action isn't possible right now.", "error");
    } else {
      setMessage($("message"), "Something went wrong talking to the server. Try again.", "error");
    }
  }

  function save() {
    const text = $("edit").value.trim();
    if (!text) {
      if (state.pendingDrop) return act("drop");
      state.pendingDrop = true;
      setMessage($("message"),
        "The box is empty. Press Save again to drop this piece, or type the idea first.", "error");
      return;
    }
    act("save", { text });
  }

  function split() {
    const box = $("edit");
    const at = box.selectionStart;
    const before = box.value.slice(0, at).trim();
    const after = box.value.slice(at).trim();
    if (!before || !after) {
      setMessage($("message"),
        "Click inside the text, between the two ideas (with words on both sides), then press Split.", "error");
      return;
    }
    act("split", { before, after });
  }

  $("btn-save").addEventListener("click", save);
  $("btn-discard").addEventListener("click", () => act("discard"));
  $("btn-combine").addEventListener("click", () => act("combine"));
  $("btn-back").addEventListener("click", () => act("back"));
  $("btn-split").addEventListener("click", split);

  $("edit").addEventListener("input", () => { state.pendingDrop = false; });
  $("edit").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      save();
    } else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "d") {
      event.preventDefault();
      act("discard");
    }
  });

  // ------------------------------------------------------- leaving / leases

  function startHeartbeat() {
    stopHeartbeat();
    state.heartbeat = setInterval(async () => {
      if (!state.group) return;
      const { status } = await api("POST", "/api/groups/" + encodeURIComponent(state.group) + "/heartbeat");
      if (status === 409) leaveReview("Another reviewer now has " + state.group + ".");
      else if (status === 401) sessionExpired();
    }, 30000);
  }

  function stopHeartbeat() {
    if (state.heartbeat) { clearInterval(state.heartbeat); state.heartbeat = null; }
  }

  async function leaveReview(notice) {
    const group = state.group;
    state.group = null;
    state.snapshot = null;
    stopHeartbeat();
    if (group && !notice) {
      await api("POST", "/api/groups/" + encodeURIComponent(group) + "/release");
    }
    await showGroups();
    if (notice) setMessage($("groups-message"), notice, "error");
  }

  $("btn-groups").addEventListener("click", () => leaveReview(null));

  window.addEventListener("pagehide", () => {
    if (state.group && navigator.sendBeacon) {
      navigator.sendBeacon(
        "/api/groups/" + encodeURIComponent(state.group) + "/release",
        new Blob(["{}"], { type: "application/json" }));
    }
  });

  boot();
})();
