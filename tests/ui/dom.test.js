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
      d.getElementById("voice-record").textContent === "Записать через бота",
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
      d.getElementById("voice-record").textContent === "Записать через бота",
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

  // --- 7. мультяшный режим на том же экране --------------------------------
  // Экран photo обслуживает два режима, и перепутать их легко: разметка
  // одна, а на сервер должно уехать разное. Проверяем оба направления.
  {
    const { w, d } = makeWindow({
      name: "Аня", job: null, toon: true, inbox: { voice: null, video: null },
    });
    await settled();
    check("кнопка мультяшного видна, когда сервер её разрешил", visible(d, "card-toon"));

    d.getElementById("card-toon").click();
    check("открылся экран photo", d.getElementById("photo").classList.contains("on"));
    check("заголовок сменился",
      d.getElementById("photo-title").textContent === "Cartoon avatar",
      d.getElementById("photo-title").textContent);
    check("появилось пояснение про рисовку", visible(d, "toon-lead"));
    check("подпись кнопки сменилась",
      d.getElementById("photo-go").textContent === "Нарисовать и оживить");

    // Возврат на обычный режим должен всё вернуть: иначе человек, заглянувший
    // в мультяшный и передумавший, молча получит мультяшного.
    d.querySelector('[data-mode="photo"]').click();
    check("заголовок вернулся",
      d.getElementById("photo-title").textContent === "Аватар по фото");
    check("пояснение спряталось", !visible(d, "toon-lead"));
    w.close();
  }

  // --- 8. без ключей Kandinsky кнопки нет -----------------------------------
  {
    const { w, d } = makeWindow({
      name: "Аня", job: null, toon: false, inbox: { voice: null, video: null },
    });
    await settled();
    check("кнопка мультяшного скрыта, когда рисовать нечем", !visible(d, "card-toon"));
    w.close();
  }

  // --- 9. портрет показывается, пока идёт видео ------------------------------
  {
    const { w, d } = makeWindow({
      name: "Аня", inbox: { voice: null, video: null },
      job: { job_id: "t1", mode: "toon", status: "running", progress: 40,
             poster_url: "/media/t1_toon.png", media_url: null, error: "" },
    });
    await settled();
    check("портрет показан", visible(d, "wait-poster"));
    check("картинка подставлена",
      d.getElementById("wait-poster-img").getAttribute("src") === "/media/t1_toon.png");
    check("подсказка про второй шаг",
      d.getElementById("wait-note").textContent.indexOf("оживляю") >= 0,
      d.getElementById("wait-note").textContent);
    w.close();
  }

  // --- 10. короткая реплика помечается красным ------------------------------
  {
    const { w, d } = makeWindow({
      name: "Аня", job: null, toon: true, inbox: { voice: null, video: null },
    });
    await settled();
    // Подменяем ответ оценщика: важно поведение окна, а не арифметика сервера.
    const area = d.getElementById("photo-text");
    const out = d.querySelector('[data-counter="photo-text"]');
    w.fetch = function (url) {
      const body = url === "/api/estimate"
        ? { speech_seconds: 1.3, limit_seconds: 14, over: false, clip_seconds: 4, short: true }
        : {};
      return Promise.resolve({ ok: true, status: 200,
        json: () => Promise.resolve(body) });
    };
    area.value = "Вот это круто!";
    area.dispatchEvent(new w.Event("input"));
    await new Promise((r) => setTimeout(r, 350));
    check("короткая реплика помечена", out.className.indexOf("warn") >= 0, out.className);
    check("сказано, почему", out.textContent.indexOf("менее стабильным") >= 0,
      out.textContent);
    // Считать числа в строке — единственный способ поймать возврат каши
    // вида «≈ 0.4 с из 14 · ролик 4 с». Человеку нужно одно сообщение.
    check("лишних чисел нет", (out.textContent.match(/\d+/g) || []).length === 0,
      out.textContent);

    // Нормальная реплика: одно число и никакой тревоги.
    w.fetch = function () {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(
        { speech_seconds: 6.2, limit_seconds: 14, over: false, clip_seconds: 7, short: false }) });
    };
    area.value = "Привет! Меня зовут Владимир, и я цифровой аватар.";
    area.dispatchEvent(new w.Event("input"));
    await new Promise((r) => setTimeout(r, 350));
    check("нормальная реплика без тревоги", out.className === "counter", out.className);
    check("одно число", (out.textContent.match(/\d+/g) || []).length === 1, out.textContent);

    // Слишком длинная: говорим, на сколько сократить, и красным.
    w.fetch = function () {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(
        { speech_seconds: 17.4, limit_seconds: 14, over: true, clip_seconds: 15, short: false }) });
    };
    area.value = "очень длинный текст";
    area.dispatchEvent(new w.Event("input"));
    await new Promise((r) => setTimeout(r, 350));
    check("длинная помечена", out.className.indexOf("over") >= 0, out.className);
    check("сказано, на сколько сократить",
      out.textContent.indexOf("4 с") >= 0, out.textContent);
    w.close();
  }

  // --- 11. про запись голоса сказано честно и сразу -------------------------
  // Микрофон вебвью MAX не даёт (проверено на телефоне: NotAllowedError).
  // Значит человек должен узнать про длинный путь ДО того, как начнёт
  // искать кнопку записи, а не после.
  {
    const { w, d } = makeWindow({
      name: "Аня", job: null, inbox: { voice: null, video: null },
    });
    await settled();
    d.querySelector('[data-voice-tab="chat"]').click();
    await settled();
    const body = d.querySelector('[data-voice-body="chat"]');
    check("предупреждение про бота на месте",
      body.textContent.indexOf("только через бота") >= 0);
    check("предложена запасная дорога",
      body.textContent.indexOf("готовый голос") >= 0);
    check("кнопки записи в окне нет", !d.getElementById("voice-capture"));
    w.close();
  }

  if (failures.length) {
    console.error("ПРОВАЛЫ:\n  " + failures.join("\n  "));
    process.exit(1);
  }
  console.log("окно: все проверки прошли");
})();
