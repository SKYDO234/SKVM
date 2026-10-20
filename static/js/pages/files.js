/* File manager: browsing, editor, uploads, chmod, shares, log tailing. */
(function () {
  "use strict";
  const SKVM = window.SKVM;

  function payload() {
    try {
      return JSON.parse(document.getElementById("files-data").textContent);
    } catch (error) {
      return { uid: "", root: "/root" };
    }
  }

  const FILE_ICONS = {
    ".txt": "file", ".log": "list", ".conf": "settings", ".cfg": "settings", ".ini": "settings",
    ".env": "key", ".sh": "code", ".bash": "code", ".py": "code", ".js": "code", ".ts": "code",
    ".json": "code", ".yaml": "code", ".yml": "code", ".toml": "code", ".md": "book", ".html": "code",
    ".css": "code", ".xml": "code", ".sql": "database", ".pem": "key", ".crt": "key", ".key": "key",
    ".pub": "key", ".tar": "archive", ".gz": "archive", ".zip": "archive", ".service": "settings",
  };

  SKVM.ready(function () {
    const data = payload();
    const base = `/files/${data.uid}`;
    const state = {
      path: data.root || "/root",
      entries: [],
      selected: new Set(),
      sort: "name",
      search: "",
      editing: null,
      dirty: false,
      tailPath: null,
      shares: [],
    };

    const rows = document.getElementById("file-rows");
    const breadcrumbs = document.getElementById("breadcrumbs");
    const countLabel = document.getElementById("file-count");
    const editor = document.getElementById("editor");
    const editorEmpty = document.getElementById("editor-empty");
    const editorMeta = document.getElementById("editor-meta");
    const editorTitle = document.getElementById("editor-title");
    const saveButton = document.getElementById("btn-save");
    const downloadButton = document.getElementById("btn-download-file");
    const usageLabel = document.getElementById("usage-label");

    function humanSize(bytes) {
      if (!bytes) return "—";
      const units = ["B", "KB", "MB", "GB", "TB"];
      let size = bytes;
      let index = 0;
      while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
      return `${size >= 100 || index < 2 ? size.toFixed(0) : size.toFixed(1)} ${units[index]}`;
    }

    function toastError(error) {
      SKVM.handleError(error);
    }

    /* ------------------------------------------------------------ render */
    function renderCrumbs(crumbs) {
      breadcrumbs.innerHTML = "";
      (crumbs || []).forEach((crumb, index) => {
        const link = document.createElement("button");
        link.type = "button";
        link.className = "crumb";
        link.textContent = crumb.name === "root" ? "/" : crumb.name;
        link.addEventListener("click", () => load(crumb.path));
        breadcrumbs.appendChild(link);
        if (index < crumbs.length - 1) {
          const separator = document.createElement("span");
          separator.className = "sep";
          separator.textContent = "/";
          breadcrumbs.appendChild(separator);
        }
      });
    }

    function sortEntries(list) {
      const sorted = list.slice();
      const by = state.sort;
      sorted.sort((a, b) => {
        if (a.type !== b.type) return a.type === "dir" ? -1 : 1;
        if (by === "size") return (b.size || 0) - (a.size || 0);
        if (by === "modified") return (b.modified || 0) - (a.modified || 0);
        if (by === "type") return String(a.extension).localeCompare(String(b.extension)) || a.name.localeCompare(b.name);
        return String(a.name).localeCompare(String(b.name), undefined, { numeric: true });
      });
      return sorted;
    }

    function render() {
      rows.innerHTML = "";
      const filtered = state.entries.filter((entry) =>
        !state.search || entry.name.toLowerCase().includes(state.search.toLowerCase()));
      const list = sortEntries(filtered);
      if (!list.length) {
        rows.innerHTML = `<tr><td colspan="6"><div class="empty tiny muted" style="padding:26px">
          ${state.search ? "No files match that search." : "This directory is empty."}</div></td></tr>`;
        countLabel.textContent = "0 entries";
        return;
      }
      list.forEach((entry) => {
        const row = document.createElement("tr");
        row.dataset.path = entry.path;
        row.dataset.type = entry.type;
        const isDir = entry.type === "dir";
        const iconName = isDir ? "folder" : (FILE_ICONS[entry.extension] || "file");
        row.innerHTML = `
          <td><input type="checkbox" data-check-file="${SKVM.escape(entry.path)}"></td>
          <td>
            <div class="file-name">
              <span class="file-ico ${isDir ? "dir" : ""}">${SKVM.icon(iconName, 15)}</span>
              <div class="truncate">
                <a href="#" class="name" data-open="${SKVM.escape(entry.path)}" data-type="${entry.type}">${SKVM.escape(entry.name)}</a>
                <div class="tiny dim truncate">${SKVM.escape(entry.path)}</div>
              </div>
            </div>
          </td>
          <td class="mono tiny">${isDir ? `${entry.children || 0} items` : humanSize(entry.size)}</td>
          <td class="mono tiny">${SKVM.escape(entry.mode || "")}</td>
          <td class="tiny">${SKVM.escape(entry.modified_human || "—")}</td>
          <td>
            <div class="row-actions">
              ${isDir ? `<button class="icon-btn ghost" data-open="${SKVM.escape(entry.path)}" data-type="dir" data-tip="Open">${SKVM.icon("folder", 14)}</button>`
                      : `<button class="icon-btn ghost" data-edit="${SKVM.escape(entry.path)}" data-tip="Edit">${SKVM.icon("file", 14)}</button>
                         <button class="icon-btn ghost" data-download="${SKVM.escape(entry.path)}" data-tip="Download">${SKVM.icon("download", 14)}</button>`}
              <button class="icon-btn ghost" data-tail="${SKVM.escape(entry.path)}" data-tip="Tail">${SKVM.icon("terminal", 14)}</button>
              <button class="icon-btn ghost" data-share="${SKVM.escape(entry.path)}" data-tip="Share link">${SKVM.icon("link", 14)}</button>
              <button class="icon-btn ghost" data-rename="${SKVM.escape(entry.path)}" data-name="${SKVM.escape(entry.name)}" data-tip="Rename">${SKVM.icon("save", 14)}</button>
              <button class="icon-btn ghost" data-delete="${SKVM.escape(entry.path)}" data-type="${entry.type}" data-tip="Delete">${SKVM.icon("trash", 14)}</button>
            </div>
          </td>`;
        rows.appendChild(row);
      });
      countLabel.textContent = `${list.length} ${list.length === 1 ? "entry" : "entries"}`;
    }

    async function load(path, options) {
      state.path = path || state.path;
      state.selected.clear();
      try {
        const response = await SKVM.get(`${base}/list?path=${encodeURIComponent(state.path)}`);
        state.entries = response.entries || [];
        renderCrumbs(response.breadcrumbs);
        render();
        const usage = response.usage || {};
        if (usageLabel) {
          usageLabel.textContent = `${humanSize(usage.used_bytes)} used of ${humanSize(usage.total_bytes)} (${usage.percent}%)`;
          const bar = document.querySelector(".usage-strip .meter i");
          if (bar) bar.style.width = Math.min(100, usage.percent || 0) + "%";
        }
        if (options && options.keepEditor === false) closeEditor();
      } catch (error) {
        toastError(error);
      }
    }

    /* ------------------------------------------------------------ editor */
    function openEditor(path, content, meta) {
      state.editing = path;
      state.dirty = false;
      editor.value = content;
      editor.classList.remove("hidden");
      editorEmpty.classList.add("hidden");
      editorMeta.classList.remove("hidden");
      editorMeta.textContent = meta || path;
      editorTitle.innerHTML = `${SKVM.icon("file", 16)} ${SKVM.escape(path.split("/").pop() || path)}`;
      saveButton.classList.remove("hidden");
      downloadButton.classList.remove("hidden");
      saveButton.disabled = true;
    }

    function closeEditor() {
      state.editing = null;
      editor.classList.add("hidden");
      editorMeta.classList.add("hidden");
      editorEmpty.classList.remove("hidden");
      saveButton.classList.add("hidden");
      downloadButton.classList.add("hidden");
      editorTitle.innerHTML = `${SKVM.icon("file", 16)} Editor`;
    }

    editor?.addEventListener("input", () => {
      state.dirty = true;
      saveButton.disabled = false;
    });
    editor?.addEventListener("keydown", (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key === "s") {
        event.preventDefault();
        save();
      }
      if (event.key === "Tab") {
        event.preventDefault();
        const start = editor.selectionStart;
        editor.setRangeText("  ", start, editor.selectionEnd, "end");
      }
    });

    async function save() {
      if (!state.editing) return;
      saveButton.classList.add("is-loading");
      try {
        const response = await SKVM.post(`${base}/write`, { path: state.editing, content: editor.value });
        SKVM.ok(response.message || "Saved");
        state.dirty = false;
        saveButton.disabled = true;
      } catch (error) {
        toastError(error);
      } finally {
        saveButton.classList.remove("is-loading");
      }
    }

    /* ------------------------------------------------------------ events */
    document.addEventListener("click", async (event) => {
      const openTarget = event.target.closest("[data-open]");
      if (openTarget) {
        event.preventDefault();
        const path = openTarget.dataset.open;
        if (openTarget.dataset.type === "dir") load(path);
        return;
      }

      const editTarget = event.target.closest("[data-edit]");
      if (editTarget) {
        const path = editTarget.dataset.edit;
        try {
          const response = await SKVM.get(`${base}/read?path=${encodeURIComponent(path)}`);
          if (response.binary) {
            SKVM.warn("Binary file — downloading instead");
            window.location.assign(`${base}/download?path=${encodeURIComponent(path)}`);
            return;
          }
          openEditor(path, response.content || "", `${humanSize(response.size)} · ${response.modified || "—"}`);
        } catch (error) {
          toastError(error);
        }
        return;
      }

      const downloadTarget = event.target.closest("[data-download]");
      if (downloadTarget) {
        window.location.assign(`${base}/download?path=${encodeURIComponent(downloadTarget.dataset.download)}`);
        return;
      }

      const tailTarget = event.target.closest("[data-tail]");
      if (tailTarget) {
        const output = document.getElementById("tail-output");
        const hint = document.getElementById("tail-hint");
        const lines = document.getElementById("tail-lines").value;
        try {
          const response = await SKVM.get(`${base}/tail?path=${encodeURIComponent(tailTarget.dataset.tail)}&lines=${lines}`);
          output.textContent = response.content || "(empty)";
          output.classList.remove("hidden");
          hint.classList.add("hidden");
          state.tailPath = tailTarget.dataset.tail;
        } catch (error) {
          toastError(error);
        }
        return;
      }

      const shareTarget = event.target.closest("[data-share]");
      if (shareTarget) {
        const path = shareTarget.dataset.share;
        const hours = await SKVM.prompt({
          title: "Create a share link",
          label: "Valid for (hours)",
          value: "24",
        });
        if (!hours) return;
        try {
          const response = await SKVM.post(`${base}/share`, { path, hours: parseInt(hours, 10) || 24 });
          const url = `${window.location.origin}/files/share/${response.share.token}`;
          SKVM.copy(url, "Share link");
          const list = document.getElementById("share-list");
          state.shares.unshift({ path, url, expires: response.share.expires_at });
          list.classList.remove("muted");
          list.innerHTML = state.shares.map((item) => `
            <div class="share-row">
              <div class="truncate">
                <b class="truncate">${SKVM.escape(item.path.split("/").pop())}</b>
                <div class="tiny dim truncate">${SKVM.escape(item.url)}</div>
              </div>
              <div class="row tight">
                <span class="badge quiet tiny">${SKVM.escape(String(item.expires || "").slice(0, 16))}</span>
                <button class="icon-btn ghost" data-copy="${SKVM.escape(item.url)}" data-tip="Copy">${SKVM.icon("copy", 13)}</button>
              </div>
            </div>`).join("");
        } catch (error) {
          toastError(error);
        }
        return;
      }

      const renameTarget = event.target.closest("[data-rename]");
      if (renameTarget) {
        const path = renameTarget.dataset.rename;
        const next = await SKVM.prompt({
          title: "Rename",
          message: path,
          label: "New name",
          value: renameTarget.dataset.name,
        });
        if (!next || next === renameTarget.dataset.name) return;
        const parent = path.slice(0, path.lastIndexOf("/")) || "/";
        try {
          await SKVM.post(`${base}/rename`, { path, destination: `${parent === "/" ? "" : parent}/${next}` });
          SKVM.ok("Renamed");
          load(state.path);
        } catch (error) { toastError(error); }
        return;
      }

      const deleteTarget = event.target.closest("[data-delete]");
      if (deleteTarget) {
        const path = deleteTarget.dataset.delete;
        const recursive = deleteTarget.dataset.type === "dir";
        const confirmed = await SKVM.confirm({
          title: "Delete file?",
          message: `${path}${recursive ? " and everything inside it" : ""} will be removed permanently.`,
          confirmLabel: "Delete",
          });
        if (!confirmed) return;
        try {
          await SKVM.post(`${base}/delete`, { path, recursive });
          SKVM.ok("Deleted");
          load(state.path);
        } catch (error) { toastError(error); }
        return;
      }

      const bulk = event.target.closest("[data-bulk-files]");
      if (bulk) {
        await runBulk(bulk.dataset.bulkFiles);
        return;
      }

      const preset = event.target.closest("[data-chmod-preset]");
      if (preset) {
        document.getElementById("chmod-input").value = preset.dataset.chmodPreset;
        return;
      }
    });

    /* -------------------------------------------------------------- bulk */
    function selectedPaths() {
      return Array.from(state.selected);
    }

    function syncBulk() {
      const has = state.selected.size > 0;
      document.querySelectorAll("[data-bulk-files]").forEach((button) => button.classList.toggle("hidden", !has));
    }

    document.addEventListener("change", (event) => {
      if (event.target.id === "check-all") {
        state.selected.clear();
        document.querySelectorAll("[data-check-file]").forEach((box) => {
          box.checked = event.target.checked;
          if (box.checked) state.selected.add(box.dataset.checkFile);
        });
        syncBulk();
      }
      const box = event.target.closest("[data-check-file]");
      if (box) {
        if (box.checked) state.selected.add(box.dataset.checkFile);
        else state.selected.delete(box.dataset.checkFile);
        syncBulk();
      }
    });

    async function runBulk(action) {
      const paths = selectedPaths();
      if (!paths.length) return;
      if (action === "download") {
        paths.forEach((path, index) => setTimeout(() => {
          window.location.assign(`${base}/download?path=${encodeURIComponent(path)}`);
        }, index * 400));
        return;
      }
      if (action === "chmod") {
        const mode = document.getElementById("chmod-input").value.trim();
        for (const path of paths) {
          await SKVM.post(`${base}/chmod`, { path, mode }).catch((error) => SKVM.handleError(error));
        }
        SKVM.ok(`chmod ${mode} applied to ${paths.length} item(s)`);
        load(state.path);
        return;
      }
      if (action === "share") {
        const hours = await SKVM.prompt({ title: "Share selected files", label: "Valid for (hours)", value: "24" });
        if (!hours) return;
        let made = 0;
        for (const path of paths) {
          try {
            const response = await SKVM.post(`${base}/share`, { path, hours: parseInt(hours, 10) || 24 });
            state.shares.unshift({ path, url: `${window.location.origin}/files/share/${response.share.token}`, expires: response.share.expires_at });
            made += 1;
          } catch (error) { /* keep going */ }
        }
        SKVM.ok(`${made} link(s) created — see the sidebar`);
        return;
      }
      if (action === "delete") {
        const confirmed = await SKVM.confirm({
          title: `Delete ${paths.length} item(s)?`,
          message: "Directories are removed recursively.",
          confirmLabel: "Delete",
          });
        if (!confirmed) return;
        for (const path of paths) {
          await SKVM.post(`${base}/delete`, { path, recursive: true }).catch((error) => SKVM.handleError(error));
        }
        SKVM.ok("Deleted");
        load(state.path);
        return;
      }
      if (action === "copy") {
        const target = await SKVM.prompt({ title: "Copy to", label: "Destination directory", value: state.path });
        if (!target) return;
        for (const path of paths) {
          const name = path.split("/").pop();
          try {
            await SKVM.post(`${base}/copy`, { path, destination: `${target.replace(/\/$/, "")}/${name}` });
          } catch (error) { SKVM.handleError(error); }
        }
        SKVM.ok("Copied");
        load(state.path);
      }
    }

    /* ----------------------------------------------------------- toolbar */
    document.getElementById("btn-refresh")?.addEventListener("click", () => load(state.path));
    document.getElementById("btn-back")?.addEventListener("click", () => {
      const parent = state.path.replace(/\/[^/]+\/?$/, "") || "/";
      load(parent);
    });
    document.getElementById("sort-select")?.addEventListener("change", (event) => {
      state.sort = event.target.value;
      render();
    });
    document.getElementById("search-input")?.addEventListener("input", SKVM.debounce((event) => {
      state.search = event.target.value.trim();
      render();
    }, 150));
    document.getElementById("btn-search")?.addEventListener("click", async () => {
      const query = document.getElementById("search-input").value.trim();
      if (!query) return SKVM.warn("Type something to search for");
      try {
        const response = await SKVM.get(`${base}/search?root=${encodeURIComponent(state.path)}&q=${encodeURIComponent(query)}`);
        state.entries = response.results || [];
        state.search = "";
        render();
        countLabel.textContent = `${response.total || state.entries.length} match(es) for “${query}”`;
      } catch (error) { toastError(error); }
    });
    document.getElementById("btn-save")?.addEventListener("click", save);
    document.getElementById("btn-download-file")?.addEventListener("click", () => {
      if (state.editing) window.location.assign(`${base}/download?path=${encodeURIComponent(state.editing)}`);
    });
    document.getElementById("btn-tail")?.addEventListener("click", () => {
      const last = state.tailPath || "/var/log/syslog";
      document.querySelector(`[data-tail="${CSS.escape(last)}"]`)?.click();
    });
    document.getElementById("tail-lines")?.addEventListener("change", () => document.getElementById("btn-tail").click());
    document.getElementById("btn-chmod")?.addEventListener("click", () => runBulk("chmod"));

    document.getElementById("btn-new-folder")?.addEventListener("click", async () => {
      const name = await SKVM.prompt({ title: "New folder", message: state.path, label: "Folder name", value: "new-folder" });
      if (!name) return;
      try {
        await SKVM.post(`${base}/mkdir`, { path: `${state.path.replace(/\/$/, "")}/${name}` });
        SKVM.ok("Folder created");
        load(state.path);
      } catch (error) { toastError(error); }
    });

    document.getElementById("btn-new-file")?.addEventListener("click", async () => {
      const name = await SKVM.prompt({ title: "New file", message: state.path, label: "File name", value: "notes.txt" });
      if (!name) return;
      const path = `${state.path.replace(/\/$/, "")}/${name}`;
      try {
        await SKVM.post(`${base}/write`, { path, content: "" });
        SKVM.ok("File created");
        await load(state.path);
        openEditor(path, "", "0 B · just now");
      } catch (error) { toastError(error); }
    });

    /* ----------------------------------------------------------- upload */
    const fileInput = document.getElementById("file-input");
    const tray = document.getElementById("upload-tray");
    const bar = document.getElementById("upload-bar");
    const detail = document.getElementById("upload-detail");
    const title = document.getElementById("upload-title");

    document.getElementById("btn-upload")?.addEventListener("click", () => fileInput.click());
    document.getElementById("upload-close")?.addEventListener("click", () => tray.classList.add("hidden"));
    fileInput?.addEventListener("change", async () => {
      const files = Array.from(fileInput.files || []);
      if (!files.length) return;
      tray.classList.remove("hidden");
      title.textContent = `Uploading ${files.length} file(s) to ${state.path}`;
      let done = 0;
      for (const file of files) {
        const form = new FormData();
        form.append("path", state.path);
        form.append("file", file, file.name);
        try {
          await SKVM.post(`${base}/upload`, form);
          done += 1;
        } catch (error) {
          SKVM.err(`${file.name}: ${error.message}`);
        }
        bar.style.width = `${Math.round((done / files.length) * 100)}%`;
        detail.textContent = `${done}/${files.length} uploaded · ${humanSize(file.size)} each`;
      }
      title.textContent = "Upload complete";
      SKVM.ok(`${done} file(s) uploaded`);
      fileInput.value = "";
      setTimeout(() => tray.classList.add("hidden"), 2500);
      load(state.path);
    });

    /* -------------------------------------------------------- drag & drop */
    const panel = document.getElementById("file-panel");
    ["dragenter", "dragover"].forEach((name) => panel?.addEventListener(name, (event) => {
      event.preventDefault();
      panel.classList.add("dragging");
    }));
    ["dragleave", "drop"].forEach((name) => panel?.addEventListener(name, (event) => {
      event.preventDefault();
      panel.classList.remove("dragging");
      if (name === "drop" && event.dataTransfer?.files?.length) {
        fileInput.files = event.dataTransfer.files;
        fileInput.dispatchEvent(new Event("change"));
      }
    }));

    window.addEventListener("beforeunload", (event) => {
      if (state.dirty) {
        event.preventDefault();
        event.returnValue = "You have unsaved edits";
      }
    });

    load(state.path, { keepEditor: false });
  });
})();
