// サーバー登録・設定のダイアログ(設計書 §3.1、モック「サーバー登録ダイアログ」)
import { call } from "./api.js";
import { clear, h } from "./dom.js";
import { t } from "./i18n.js";
import { confirmDialog, modal } from "./ui.js";

let uid = 0;
function field(label, input, help = null) {
  const id = `f${++uid}`;
  input.id = id;
  return [h("label", { for: id }, label), input, help ? h("div", { class: "help" }, help) : null];
}

function radio(name, value, label, checked) {
  return h("label", {}, h("input", { type: "radio", name, value, checked }), label);
}

export function serverDialog(app, server = null) {
  const s = server || { name: "", url: "", textFormat: "common_mark", pollInterval: 180, notifyEnabled: true,
    proxyMode: "system", proxyUrl: "", enabled: true };
  const group = `g${++uid}`;
  const name = h("input", { type: "text", value: s.name, autocomplete: "off" });
  const url = h("input", { type: "url", value: s.url, placeholder: "https://redmine.example.com", autocomplete: "off" });
  const key = h("input", { type: "password", autocomplete: "off",
    placeholder: server ? t("server.keyUnchanged") : "" });
  const result = h("div", { class: "result", role: "status", hidden: true });
  const testButton = h("button", { type: "button", class: "btn" }, t("server.test"));
  const guess = h("div", { class: "note" }, t("server.formatHelp"));
  const formats = [
    radio(`${group}-fmt`, "common_mark", t("server.markdown"), s.textFormat === "common_mark"),
    radio(`${group}-fmt`, "textile", "Textile", s.textFormat === "textile"),
    radio(`${group}-fmt`, "plain", t("server.plain"), s.textFormat === "plain"),
  ];
  const poll = h("select", {}, [180, 300].map((sec) =>
    h("option", { value: sec, selected: sec === s.pollInterval }, t("server.minutes", { count: sec / 60 }))));
  const notify = h("input", { type: "checkbox", checked: s.notifyEnabled });
  const proxySystem = h("input", { type: "radio", name: `${group}-proxy`, checked: s.proxyMode !== "manual" });
  const proxyManual = h("input", { type: "radio", name: `${group}-proxy`, checked: s.proxyMode === "manual" });
  const proxyUrl = h("input", { type: "text", value: s.proxyUrl || "", placeholder: "http://proxy.example.com:8080",
    disabled: s.proxyMode !== "manual", "aria-label": t("server.proxyUrl") });
  const enabled = h("input", { type: "checkbox", checked: s.enabled });
  const save = h("button", { type: "button", class: "btn primary" }, server ? t("common.save") : t("server.add"));

  let probe = null;
  let testedWith = server ? `${s.url}\n` : null; // 編集時は、URLとキーを変えなければ再テスト不要
  const inputs = () => `${url.value.trim().replace(/\/+$/, "")}\n${key.value.trim()}`;
  const update = () => {
    testButton.disabled = !url.value.trim() || (!key.value.trim() && !server);
    save.disabled = !name.value.trim() || testedWith !== inputs();
  };
  const show = (kind, text) => {
    result.hidden = false;
    result.className = `result ${kind}`;
    result.textContent = text;
  };
  for (const el of [name, url, key]) el.addEventListener("input", update);
  proxySystem.addEventListener("change", () => (proxyUrl.disabled = true));
  proxyManual.addEventListener("change", () => (proxyUrl.disabled = false));

  testButton.addEventListener("click", async () => {
    const tested = inputs();
    testButton.disabled = true;
    show("busy", t("server.connecting"));
    try {
      probe = await call("testConnection", {
        url: url.value.trim(), apiKey: key.value.trim(), proxyMode: proxyManual.checked ? "manual" : "system",
        proxyUrl: proxyUrl.value.trim() || null, serverId: server?.serverId ?? null,
      });
      testedWith = tested;
      show("ok", t("server.connected", { name: probe.name, id: probe.userId, groups: probe.groupIds.length,
        assigned: probe.assignedCount, watched: probe.watchedCount }));
      const g = probe.formatGuess;
      if (g && g.textFormat) {
        guess.textContent = t("server.guessed", { name: g.textFormat === "textile" ? "Textile" : "Markdown", n: g.samples });
        if (!server) for (const r of formats) r.querySelector("input").checked = r.querySelector("input").value === g.textFormat;
      } else {
        guess.textContent = t("server.notGuessed");
      }
      if (!name.value.trim()) name.value = probe.url.replace(/^https?:\/\//, "");
    } catch (e) {
      probe = null;
      testedWith = null;
      show("error", t(`errors.${e.code}`));
    }
    update();
  });

  save.addEventListener("click", async () => {
    save.disabled = true;
    const data = {
      serverId: server?.serverId ?? null, name: name.value.trim(), url: url.value.trim(),
      apiKey: key.value.trim() || null,
      textFormat: formats.map((r) => r.querySelector("input")).find((i) => i.checked).value,
      pollInterval: Number(poll.value), notifyEnabled: notify.checked, enabled: enabled.checked,
      proxyMode: proxyManual.checked ? "manual" : "system", proxyUrl: proxyUrl.value.trim() || null,
    };
    if (probe) Object.assign(data, { userId: probe.userId, login: probe.login, userName: probe.name, groupIds: probe.groupIds });
    try {
      await call("saveServer", { data });
      m.close();
      app.onServersChanged();
    } catch (e) {
      show("error", t(`errors.${e.code}`));
      update();
    }
  });

  const body = h("div", { class: "dialog-body" },
    h("div", { class: "form" },
      field(t("server.name"), name),
      field("URL", url),
      field(t("server.apiKey"), key, t("server.apiKeyHelp"))),
    h("div", { class: "test-row" }, testButton, result),
    h("fieldset", {}, h("legend", {}, t("server.textFormat")), guess, formats),
    h("fieldset", {}, h("legend", {}, t("server.syncAndNotify")),
      h("div", { class: "form" },
        field(t("server.pollInterval"), poll),
        h("span", { class: "label" }, t("server.notifications")), h("label", {}, notify, t("server.notifyNew")),
        h("span", { class: "label" }, t("server.proxy")),
        h("div", { class: "inline" }, h("label", {}, proxySystem, t("server.proxySystem")),
          h("label", {}, proxyManual, t("server.proxyManual")), proxyUrl))),
    h("label", {}, enabled, " ", t("server.enabled")));
  const cancel = h("button", { type: "button", class: "btn", onClick: () => m.close() }, t("common.cancel"));
  const m = modal({ title: server ? t("server.editTitle") : t("server.addTitle"), body, footer: [cancel, save] });
  update();
  return m;
}

export function settingsDialog(app) {
  app.settingsDialog?.close(); // 二重に開かない
  const tabs = h("div", { class: "dialog-tabs tabs", role: "tablist" });
  const panel = h("div", { class: "dialog-body" });
  const set = (key, value) => app.saveSetting(key, value);

  // タブを切り替えて描き直すたびに、その時点の設定を読む(開いたときの値のままにしない)
  const select = (key, options, onChange = null) => {
    const el = h("select", { onChange: () => { set(key, el.value); onChange?.(el.value); } },
      options.map(([v, label]) => h("option", { value: v, selected: app.settings[key] === v }, label)));
    return el;
  };
  const check = (key, label) => h("label", {},
    h("input", { type: "checkbox", checked: !!app.settings[key], onChange: (e) => set(key, e.target.checked) }), label);

  const general = () => {
    const s = app.settings;
    const note = h("div", { class: "help", hidden: true }, t("settings.languageNote"));
    const langs = [["system", t("settings.followSystem")], ...Object.entries(app.boot.languages)];
    const retention = h("input", { type: "number", min: "0", max: "365", value: s.closed_retention_days,
      onChange: () => set("closed_retention_days", Math.max(0, Math.min(365, Number(retention.value) || 0))) });
    return h("div", { class: "form" },
      field(t("settings.language"), select("language", langs, () => { note.hidden = false; })), note,
      field(t("settings.appearance"), select("theme", [["system", t("settings.followSystem")], ["light", t("settings.light")], ["dark", t("settings.dark")]])),
      field(t("settings.myComments"), select("mine_side", [["right", t("settings.right")], ["left", t("settings.left")]])),
      h("span", { class: "label" }, t("settings.notifications")), check("notify_enabled", t("settings.showNotifications")),
      h("span", { class: "label" }, t("settings.startup")), check("autostart", t("settings.autostart")),
      field(t("settings.retention"), retention, t("settings.retentionHelp")),
      h("span", { class: "label" }, t("settings.images")), check("allow_external_images", t("settings.externalImages")),
      field(t("settings.redmineOpenIn"), select("redmine_open_in", [["app", t("settings.openInApp")], ["browser", t("settings.openInBrowser")]]),
        t("settings.redmineOpenInHelp")));
  };

  const servers = () => {
    const list = h("ul", { class: "server-list" });
    const wrap = h("div", { style: { display: "flex", flexDirection: "column", gap: "10px" } },
      list, h("div", {}, h("button", { type: "button", class: "btn", onClick: () => app.openServerDialog() }, t("settings.addServer"))));
    call("listServers").then((items) => {
      if (!items.length) list.append(h("li", { class: "muted" }, t("tree.noServers")));
      for (const sv of items) {
        list.append(h("li", {},
          h("div", { class: "info" }, h("div", {}, sv.enabled ? sv.name : t("settings.disabledName", { name: sv.name })),
            h("div", { class: "url" }, sv.url)),
          h("button", { type: "button", class: "btn", onClick: () => app.openServerDialog(sv) }, t("common.edit")),
          h("button", { type: "button", class: "btn danger", onClick: async () => {
            if (await confirmDialog(t("settings.deleteTitle"), t("settings.deleteBody", { name: sv.name }), t("common.delete"), { danger: true })) {
              await call("deleteServer", { serverId: sv.serverId });
              app.onServersChanged(); // 一覧はこの中で描き直される
            }
          } }, t("common.delete"))));
      }
    });
    return wrap;
  };

  const pages = { general: [t("settings.general"), general], servers: [t("settings.servers"), servers] };
  const show = (name) => {
    for (const b of tabs.children) b.setAttribute("aria-selected", String(b.dataset.page === name));
    clear(panel).append(pages[name][1]());
  };
  for (const [name, [label]] of Object.entries(pages)) {
    tabs.append(h("button", { type: "button", role: "tab", dataset: { page: name }, onClick: () => show(name) }, label));
  }
  const close = h("button", { type: "button", class: "btn", onClick: () => m.close() }, t("common.close"));
  const m = modal({ title: t("settings.title"), body: h("div", {}, tabs, panel), footer: [close],
    onClose: () => { if (app.settingsDialog === handle) app.settingsDialog = null; } });
  const handle = { refresh: () => show("servers"), close: () => m.close() };
  show("general");
  app.settingsDialog = handle;
  return m;
}
