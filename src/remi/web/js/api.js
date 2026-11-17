// バックエンド(ローカルAPIサーバー)との通信(設計書 §2.6)

export class RpcError extends Error {
  constructor(code, detail = "") {
    super(code);
    this.code = code;
    this.detail = detail;
  }
}

export async function call(method, params = {}) {
  let res;
  try {
    res = await fetch(`/api/${method}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params),
      credentials: "same-origin",
    });
  } catch (e) {
    throw new RpcError("backend_unreachable", String(e));
  }
  let body = null;
  try {
    body = await res.json();
  } catch {
    throw new RpcError(res.ok ? "invalid_response" : `http_${res.status}`);
  }
  if (body.error) throw new RpcError(body.error.code, body.error.detail || "");
  return body.result;
}

// サーバーからのイベント(SSE)。切れても自動で再接続し、再接続したら resync を流す。
export function subscribe(onEvent) {
  let es = null;
  let opened = false;
  const connect = () => {
    es = new EventSource("/events");
    es.onopen = () => {
      if (opened) onEvent({ type: "resync" });
      opened = true;
    };
    es.onmessage = (m) => {
      try {
        onEvent(JSON.parse(m.data));
      } catch (e) {
        console.error("bad event", e);
      }
    };
  };
  connect();
  return () => es && es.close();
}
