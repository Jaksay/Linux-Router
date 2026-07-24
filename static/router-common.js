(() => {
  const operationPollTimeoutMs = 6 * 60 * 1000;

  window.routerHttp = {
    async postForm(url, formData, options = {}) {
      const response = await fetch(url, {
        method: "POST",
        body: formData,
        credentials: "same-origin",
        headers: { "X-Requested-With": "fetch" },
      });
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      const result = await response.json();
      if (!result.pending || !result.operation_url) return result;

      const deadline = Date.now() + operationPollTimeoutMs;
      while (true) {
        if (Date.now() >= deadline) {
          throw new Error("Operation polling timed out");
        }
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
        const operationResponse = await fetch(result.operation_url, {
          credentials: "same-origin",
          headers: { "X-Requested-With": "fetch" },
        });
        if (!operationResponse.ok) {
          throw new Error(`HTTP ${operationResponse.status}`);
        }
        const operation = await operationResponse.json();
        if (
          operation.pending
          && operation.progress_message
          && typeof options.onProgress === "function"
        ) {
          options.onProgress(operation.progress_message);
        }
        if (!operation.pending) return operation;
      }
    },
  };

  window.routerFeedback = {
    scoped(key) {
      return (message, type = "success", autoHideMs = 0) => {
        if (!window.routerToast) return;
        if (!message) {
          window.routerToast.clear(key);
          return;
        }
        window.routerToast.show(message, type, { key, autoHideMs });
      };
    },
  };

  document.querySelectorAll("[data-agent-operation-form]").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = form.querySelector('button[type="submit"]');
      if (button) button.disabled = true;
      try {
        const result = await window.routerHttp.postForm(
          form.getAttribute("action"),
          new FormData(form),
        );
        if (window.routerToast) {
          window.routerToast.show(result.message || "操作已结束", result.ok ? "success" : "error", {
            key: "agent-operation",
            autoHideMs: result.ok ? 2000 : 0,
          });
        }
        window.setTimeout(() => window.location.reload(), result.ok ? 700 : 1500);
      } catch {
        if (window.routerToast) {
          window.routerToast.show("系统操作失败", "error", { key: "agent-operation" });
        }
        if (button) button.disabled = false;
      }
    });
  });
})();
