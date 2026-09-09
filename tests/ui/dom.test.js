/* Проверка окна в поддельном браузере.
 *
 * Интерфейс дважды ломался незаметно: то кнопка не возвращалась, то слот
 * не исчезал. Логика простая, но состояний много, и глазами их все не
 * обойти. Здесь index.html и app.js грузятся в jsdom с подставным fetch,
 * и проверяется ровно то, что видно человеку.
 *
 * Запускается из test.bat через tests/test_ui.py, если есть node и jsdom.
 */
const fs = require("fs");
const path = require("path");
const { JSDOM } = require(process.env.JSDOM_PATH || "jsdom");

const STATIC = path.join(__dirname, "..", "..", "src", "avatar_miniapp", "static");
const failures = [];

function check(name, condition, detail) {
  if (!condition) failures.push(name + (detail ? " — " + detail : ""));
}

function makeWindow(state) {
  const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
  const dom = new JSDOM(html, { runScripts: "outside-only", url: "https://x/" });
  const w = dom.window;

  const calls = [];
  w.fetch = function (url, opts) {
    opts = opts || {};
    calls.push({ url: url, method: opts.method || "GET" });
    let body = {};
    if (url === "/api/state") body = state;
    else if (url === "/api/voices") body = { voices: [{ id: "anya", title: "Аня", note: "" }] };
    else if (url.indexOf("/api/inbox/") === 0) body = { ok: true };
    return Promise.resolve({
      ok: true, status: 200, json: function () { return Promise.resolve(body); },
    });
  };
  w.eval(fs.readFileSync(path.join(STATIC, "app.js"), "utf8"));
  return { w, d: w.document, calls };
}

const settled = () => new Promise((r) => setTimeout(r, 30));

function visible(d, id) {
  const el = d.getElementById(id);
  return el && !el.hidden;
}

(async function () {
  // --- 1. пустое состояние -------------------------------------------------
  {
    const { w, d } = makeWindow({ name: "Аня", job: null, inbox: { voice: null, video: null } });
    await settled();
    check("стартуем на экране выбора", d.getElementById("pick").classList.contains("on"));
    check("слот голоса скрыт", !visible(d, "voice-slot"));
    check("кнопка записи видна", visible(d, "voice-record"));
    check("строка ожидания скрыта", !visible(d, "voice-armed"));
    check("подпись кнопки исходная",
      d.getElementById("voice-record").textContent === "Записать голос",
      d.getElementById("voice-record").textContent);
    w.close();
  }

  // --- 2. голос уже принят -------------------------------------------------
  {
    const { w, d } = makeWindow({
      name: "Аня", job: null,
      inbox: { voice: { seconds: 6.5, age_s: 40, name: "voice.wav" }, video: null },
    });
    await settled();
    check("слот голоса показан", visible(d, "voice-slot"));
    check("в слоте длительность",
      d.querySelector("#voice-slot .slot-text").textContent.indexOf("6.5") >= 0,
      d.querySelector("#voice-slot .slot-text").textContent);
    check("кнопка предлагает перезапись",
      d.getElementById("voice-record").textContent === "Записать заново");
    w.close();
  }

  // --- 3. крестик очищает форму, а не только кнопку -------------------------
  {
    const { w, d, calls } = makeWindow({
      name: "Аня", job: null,
      inbox: { voice: { seconds: 6.5, age_s: 40, name: "voice.wav" }, video: null },
    });
    await settled();
    d.querySelector('[data-drop="voice"]').click();
    await settled();
    check("крестик сходил на сервер",
      calls.some((c) => c.method === "DELETE" && c.url === "/api/inbox/voice"));
    check("слот исчез после крестика", !visible(d, "voice-slot"));
    check("кнопка вернулась", visible(d, "voice-record"));
    check("подпись кнопки сброшена",
      d.getElementById("voice-record").textContent === "Записать голос",
      d.getElementById("voice-record").textContent);
    w.close();
  }

  // --- 4. «записать» переводит в ожидание -----------------------------------
  {
    const { w, d, calls } = makeWindow({ name: "Аня", job: null, inbox: { voice: null, video: null } });
    await settled();
    d.getElementById("voice-record").click();
    await settled();
    check("окно предупредило сервер",
      calls.some((c) => c.method === "POST" && c.url === "/api/inbox/expect"));
    check("показана инструкция", visible(d, "voice-armed"));
    check("кнопка скрыта, пока ждём", !visible(d, "voice-record"));
    w.close();
  }

  // --- 5. видео из чата и выбор файла ---------------------------------------
  {
    const { w, d } = makeWindow({
      name: "Аня", job: null,
      inbox: { voice: null, video: { seconds: 5.2, age_s: 10, name: "video.mp4" } },
    });
    await settled();
    check("слот видео показан", visible(d, "video-slot"));
    d.querySelector('[data-drop="video"]').click();
    await settled();
    check("слот видео исчез", !visible(d, "video-slot"));
    check("кнопка видео вернулась", visible(d, "video-record"));
    check("подпись кнопки видео сброшена",
      d.getElementById("video-record").textContent === "Записать видео");
    w.close();
  }

  // --- 6. незавершённая задача возвращает на экран ожидания ------------------
  {
    const { w, d } = makeWindow({
      name: "Аня", inbox: { voice: null, video: null },
      job: { job_id: "j1", status: "running", progress: 40, media_url: null, error: "" },
    });
    await settled();
    check("вернулись в ожидание", d.getElementById("wait").classList.contains("on"));
    w.close();
  }

  if (failures.length) {
    console.error("ПРОВАЛЫ:\n  " + failures.join("\n  "));
    process.exit(1);
  }
  console.log("окно: все проверки прошли");
})();
