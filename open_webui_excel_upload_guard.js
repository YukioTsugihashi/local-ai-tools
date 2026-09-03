// Excel files are consumed directly from Open WebUI's file store by the
// excel_analysis Pipe.  Upload them without extraction/vector indexing so an
// attachment never starts an unnecessary Ollama embedding job.
(() => {
  // 二重に読み込まれても包み直さない。index.html へ直接埋め込む形と、
  // /static から読む従来の形が同時に生きうるため(2026-08-18)
  if (window.__MP_EXCEL_UPLOAD_GUARD__) return;
  window.__MP_EXCEL_UPLOAD_GUARD__ = true;

  const originalFetch = window.fetch.bind(window);
  const excelPattern = /\.(?:xlsx|xlsm)$/i;
  // 出席者名簿は例外。議事録はこのテキストを使うので、抽出を止めると
  // 中身0文字で届き、名簿なしの議事録ができる(2026-08-18に実測)。
  // 名簿は数KBなので、埋め込みが走っても負担にならない。
  const rosterPattern = /名簿|出席者|参加者|メンバー|roster/i;

  window.fetch = (input, init = {}) => {
    try {
      const requestUrl =
        typeof input === "string" || input instanceof URL ? String(input) : input.url;
      const method = String(init.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
      const body = init.body;
      if (method === "POST" && /\/api\/v1\/files\/?(?:\?|$)/.test(requestUrl) && body instanceof FormData) {
        const file = body.get("file");
        if (file instanceof File && excelPattern.test(file.name) && !rosterPattern.test(file.name)) {
          const url = new URL(requestUrl, window.location.origin);
          url.searchParams.set("process", "false");
          input = url.toString();
        }
      }
    } catch (error) {
      console.warn("Excel upload RAG guard could not inspect the request", error);
    }
    return originalFetch(input, init);
  };
})();
