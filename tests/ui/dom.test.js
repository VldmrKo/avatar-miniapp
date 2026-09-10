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
    return Promise.resolve({
      ok: true, status: 200, json: function () { return Promise.resolve(body); },
    });
  };
  w.eval(fs.readFileSync(path.join(STATIC, "app.js"), "utf8"));
  return { w, d: w.document, calls };
}

const settled = () => new Promise((r) => setTimeout(r, 30));

// jsdom не даёт положить файл в input руками — подменяем сам список.
// Нам важно только «файл выбран», содержимое роли не играет.
function fill(w, input, name) {
  Object.defineProperty(input, "files", {
    configurable: true,
    value: [new w.File(["x"], name)],
  });
  input.dispatchEvent(new w.Event("change"));
}

function visible(d, id) {
  const el = d.getElementById(id);
  return el && !el.hidden;
}

(async function () {
  // --- 1. пустое состояние -------------------------------------------------
  {
    const { w, d } = makeWindow({ name: "Аня", job: null });
    await settled();
    check("стартуем на экране выбора", d.getElementById("pick").classList.contains("on"));
    check("кнопки записи через бота больше нет", !d.getElementById("voice-record"));
    check("кнопки записи видео через бота больше нет", !d.getElementById("video-record"));
    check("слот видео скрыт", !visible(d, "video-slot"));
    w.close();
  }

  // --- 2. порядок вкладок голоса -------------------------------------------
  // «Свой голос» уехал третьим намеренно: это единственная вкладка, из
  // которой в окне ничего не сделать, и стоять первой она не должна.
  {
    const { w, d } = makeWindow({ name: "Аня", job: null });
    await settled();
    const tabs = Array.from(d.querySelectorAll("[data-voice-tab]"))
      .map((b) => b.getAttribute("data-voice-tab"));
    check("порядок вкладок: готовый, файл, свой", tabs.join(",") === "preset,file,chat",
      tabs.join(","));
    check("первой открыта «готовый»",
      d.querySelector('[data-voice-tab="preset"]').classList.contains("on"));
    w.close();
  }

  // --- 3. кнопка неактивна, пока не заполнено всё ---------------------------
  // Раньше форму можно было отправить пустой и получить ошибку в ответ.
  // Отказ после нажатия человек читает как поломку; неактивная кнопка
  // говорит то же самое, но заранее и без обвинений.
  {
    const { w, d } = makeWindow({ name: "Аня", job: null });
    await settled();
    const go = d.querySelector('[data-submit="photo"]');
    check("сразу неактивна", go.disabled);

    fill(w, d.getElementById("photo-file"), "face.png");
    check("одного фото мало", go.disabled);

    const area = d.getElementById("photo-text");
    area.value = "Привет!";
    area.dispatchEvent(new w.Event("input"));
    check("фото и текст при готовом голосе — уже можно", !go.disabled);

    // «Свой голос» в окне не заполняется ничем, значит и отправлять нечего.
    d.querySelector('[data-voice-tab="chat"]').click();
    check("на вкладке «свой голос» кнопка гаснет", go.disabled);

    d.querySelector('[data-voice-tab="file"]').click();
    check("на вкладке «файл» без файла тоже гаснет", go.disabled);
    fill(w, d.getElementById("voice-file"), "voice.wav");
    check("с файлом голоса снова можно", !go.disabled);

    area.value = "   ";
    area.dispatchEvent(new w.Event("input"));
    check("пробелы за текст не считаются", go.disabled);
    w.close();
  }

  // --- 4. видео: кнопка ждёт файл и реплику ---------------------------------
  {
    const { w, d } = makeWindow({ name: "Аня", job: null });
    await settled();
    const go = d.querySelector('[data-submit="video"]');
    check("видео: сразу неактивна", go.disabled);

    const area = d.getElementById("video-text");
    area.value = "Привет!";
    area.dispatchEvent(new w.Event("input"));
    check("одного текста мало", go.disabled);

    const input = d.getElementById("video-file");
    fill(w, input, "clip.mp4");
    input.dispatchEvent(new w.Event("change"));
    check("слот показывает выбранное", visible(d, "video-slot"));
    check("с файлом и текстом можно", !go.disabled);

    d.querySelector('[data-drop="video"]').click();
    check("крестик убрал слот", !visible(d, "video-slot"));
    check("и снова гасит кнопку", go.disabled);
    w.close();
  }

  // --- 6. незавершённая задача возвращает на экран ожидания ------------------
  {
    const { w, d } = makeWindow({
      name: "Аня",
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
      name: "Аня", job: null, toon: true,
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
      name: "Аня", job: null, toon: false,
    });
    await settled();
    check("кнопка мультяшного скрыта, когда рисовать нечем", !visible(d, "card-toon"));
    w.close();
  }

  // --- 9. портрет показывается, пока идёт видео ------------------------------
  {
    const { w, d } = makeWindow({
      name: "Аня",
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
      name: "Аня", job: null, toon: true,
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
  // Микрофон вебвью MAX не даёт (проверено на телефоне: NotAllowedError),
  // и обмен файлами с ботом мы убрали как слишком длинный путь. Значит
  // единственное, что окно тут может, — честно сказать, куда идти.
  {
    const { w, d } = makeWindow({ name: "Аня", job: null });
    await settled();
    d.querySelector('[data-voice-tab="chat"]').click();
    await settled();
    const body = d.querySelector('[data-voice-body="chat"]');
    check("сказано, что запись только через бота",
      body.textContent.indexOf("только через бота") >= 0);
    check("сказано, что делать",
      body.textContent.indexOf("Вернитесь в чат") >= 0);
    check("поля для образца нет", !d.getElementById("voice-slot"));
    check("кнопки записи нет", !d.getElementById("voice-record"));
    check("инструкции «что снять» нет",
      body.textContent.indexOf("Что снять") < 0);
    w.close();
  }

  if (failures.length) {
    console.error("ПРОВАЛЫ:\n  " + failures.join("\n  "));
    process.exit(1);
  }
  console.log("окно: все проверки прошли");
})();
