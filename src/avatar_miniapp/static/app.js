/* Логика окна.
 *
 * Три вещи, которые здесь важнее остального:
 *  - объект бриджа называется window.WebApp, а не Telegram.WebApp, и вне MAX
 *    его нет вообще (локально в браузере всё работает без него);
 *  - на экране ожидания кнопку «назад» НЕ занимаем: незанятую обрабатывает
 *    сам MAX и закрывает окно, а другого способа его закрыть в API нет;
 *  - при открытии окна первым делом спрашиваем /api/state: человек мог
 *    закрыть окно в середине генерации и вернуться.
 */
(function () {
  "use strict";

  var WA = window.WebApp || null;
  var INIT = WA && WA.initData ? WA.initData : "";
  var voice = { mode: "preset", id: "" };
  var inbox = { voice: null, video: null };
  var pickedVideo = null;
  // Бот прикладывает к ответу кнопку с payload — по ней открываем
  // сразу тот экран, с которого человек уходил записывать.
  var startAt = (WA && WA.initDataUnsafe && WA.initDataUnsafe.start_param) || "";
  var poll = null;
  var screen = "boot";
  var armedScreen = false;

  // --- сеть -----------------------------------------------------------------

  function api(path, opts) {
    opts = opts || {};
    opts.headers = opts.headers || {};
    opts.headers["X-Init-Data"] = INIT;
    return fetch(path, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (r.ok) return data;
        var err = new Error(data.error || ("HTTP " + r.status));
        err.status = r.status;   // 409 — это не ошибка, а «у тебя уже идёт»
        err.data = data;
        throw err;
      });
    });
  }

  // --- экраны ---------------------------------------------------------------

  function show(name) {
    screen = name;
    var all = document.querySelectorAll(".screen");
    for (var i = 0; i < all.length; i++) all[i].classList.remove("on");
    var el = document.getElementById(name);
    if (el) el.classList.add("on");

    if (!WA || !WA.BackButton) return;
    if (!armedScreen && (name === "photo" || name === "video")) {
      WA.BackButton.show();
    } else {
      // На pick и на ожидании кнопку отдаём системе: там она закрывает окно.
      WA.BackButton.hide();
    }
  }

  if (WA && WA.BackButton && WA.BackButton.onClick) {
    WA.BackButton.onClick(function () {
      // Событие на части платформ прилетает и при скрытой кнопке,
      // поэтому смотрим текущий экран, а не доверяем видимости.
      // Пока ждём запись, «назад» должен закрывать окно, а не уводить
      // на выбор режима: человек как раз идёт в чат записывать.
      if (!armedScreen && (screen === "photo" || screen === "video")) show("pick");
    });
  }

  // --- счётчик секунд -------------------------------------------------------
  // Показываем секунды, а не символы: ограничивает нас длительность речи,
  // а не длина строки, и человеку так понятнее, почему нельзя больше.

  function wireCounter(id) {
    var area = document.getElementById(id);
    var out = document.querySelector('[data-counter="' + id + '"]');
    var timer = null;
    area.addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        if (!area.value.trim()) { out.textContent = " "; out.className = "counter"; return; }
        api("/api/estimate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: area.value })
        }).then(function (r) {
          out.textContent = "≈ " + r.speech_seconds + " с из " + r.limit_seconds;
          out.className = r.over ? "counter over" : "counter";
        }).catch(function () { out.textContent = " "; });
      }, 250);
    });
  }

  // --- голос ----------------------------------------------------------------

  function wireVoice() {
    var tabs = document.querySelectorAll("[data-voice-tab]");
    for (var i = 0; i < tabs.length; i++) {
      tabs[i].addEventListener("click", function () {
        var name = this.getAttribute("data-voice-tab");
        voice.mode = name;
        var all = document.querySelectorAll("[data-voice-tab]");
        for (var j = 0; j < all.length; j++) all[j].classList.remove("on");
        this.classList.add("on");
        var bodies = document.querySelectorAll("[data-voice-body]");
        for (var k = 0; k < bodies.length; k++) {
          bodies[k].classList.toggle("on", bodies[k].getAttribute("data-voice-body") === name);
        }
        // Человек мог записать голосовое, не закрывая окно, и вернуться
        // сюда переключением вкладки — перечитываем состояние.
        if (name === "chat") refreshInbox();
      });
    }

    api("/api/voices").then(function (r) {
      var box = document.getElementById("voice-list");
      box.innerHTML = "";
      if (!r.voices.length) {
        box.innerHTML = '<div class="hint">Готовых голосов пока нет — '
                      + 'запишите свой или загрузите файл.</div>';
        return;
      }
      r.voices.forEach(function (v, n) {
        var b = document.createElement("button");
        b.className = "chip" + (n === 0 ? " on" : "");
        b.innerHTML = "<b>" + v.title + "</b><small>" + v.note + "</small>";
        b.addEventListener("click", function () {
          var all = box.querySelectorAll(".chip");
          for (var i = 0; i < all.length; i++) all[i].classList.remove("on");
          b.classList.add("on");
          voice.id = v.id;
        });
        if (n === 0) voice.id = v.id;
        box.appendChild(b);
      });
    });
  }

  // --- что уже прислано боту в чат -------------------------------------------
  // Окну MAX не даёт ни микрофон, ни камеру в режиме видео. Записывает сам
  // мессенджер, а окно показывает, что принято.

  function ago(seconds) {
    if (seconds < 60) return "только что";
    var minutes = Math.round(seconds / 60);
    if (minutes < 60) return minutes + " мин назад";
    return Math.round(minutes / 60) + " ч назад";
  }

  function refreshInbox() {
    api("/api/state").then(function (state) {
      inbox = state.inbox || { voice: null, video: null };
      showInbox();
    }).catch(function () {});
  }

  function arm(kind) {
    return api("/api/inbox/expect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind: kind })
    }).then(function () {
      // Отпускаем «назад» системе: закрыть окно программно в API MAX нечем,
      // а незанятую кнопку мессенджер обрабатывает сам и окно закрывает.
      if (WA && WA.BackButton) WA.BackButton.hide();
      armedScreen = true;
      // Запись просят перезаписать — старую с экрана убираем сразу,
      // иначе непонятно, ждём мы новую или уже нет.
      inbox[kind] = null;
      if (kind === "video") { pickedVideo = null; }
      showInbox();
    }).catch(function (e) {
      document.getElementById(kind === "voice" ? "voice-armed" : "video-armed").textContent =
        "Не получилось: " + e.message;
    });
  }

  function drop(kind) {
    // Крестик должен убирать запись целиком: и с сервера, и с экрана.
    // Раньше ошибка молча съедалась, и человек видел, что ничего не изменилось.
    return api("/api/inbox/" + kind, { method: "DELETE" }).then(function () {
      inbox[kind] = null;
      if (kind === "video") {
        pickedVideo = null;
        document.getElementById("video-file").value = "";
      }
      armedScreen = false;
      showInbox();
    }).catch(function (e) {
      var line = document.getElementById(kind === "voice" ? "voice-armed" : "video-armed");
      line.hidden = false;
      line.textContent = "Не удалось удалить: " + e.message;
    });
  }

  function wireInboxButtons() {
    document.getElementById("voice-record").addEventListener("click", function () {
      arm("voice");
    });
    document.getElementById("video-record").addEventListener("click", function () {
      arm("video");
    });
    document.querySelectorAll("[data-drop]").forEach(function (b) {
      b.addEventListener("click", function () { drop(b.getAttribute("data-drop")); });
    });
    var input = document.getElementById("video-file");
    input.addEventListener("change", function () {
      pickedVideo = input.files[0] || null;
      showVideoState();
    });
  }

  // Оба блока показываются одинаково, поэтому и рисуются одной функцией:
  // раньше они разошлись, и в одном я забыл вернуть кнопку и спрятать
  // строку ожидания. Ровно такие несимметричности и вылезают на экране.
  function paintSlot(prefix, filled, caption, idleLabel, againLabel) {
    var slot = document.getElementById(prefix + "-slot");
    var record = document.getElementById(prefix + "-record");
    var armedLine = document.getElementById(prefix + "-armed");

    slot.hidden = !filled;
    if (filled) slot.querySelector(".slot-text").textContent = caption;

    // Строка «жду запись» имеет смысл, только пока мы правда ждём.
    var waiting = armedScreen && !filled;
    armedLine.hidden = !waiting;
    record.hidden = waiting;
    record.textContent = filled ? againLabel : idleLabel;
  }

  function showInbox() {
    paintSlot(
      "voice",
      !!inbox.voice,
      inbox.voice ? "Ваш голос: " + inbox.voice.seconds + " с, " + ago(inbox.voice.age_s) : "",
      "Записать голос",
      "Записать заново"
    );
    showVideoState();
  }

  function showVideoState() {
    var caption = "";
    if (pickedVideo) caption = "Выбрано: " + pickedVideo.name;
    else if (inbox.video) {
      caption = "Ваше видео: " + inbox.video.seconds + " с, " + ago(inbox.video.age_s);
    }
    paintSlot("video", !!caption, caption, "Записать видео", "Записать заново");
  }

  // --- отправка -------------------------------------------------------------

  function submit(mode) {
    var err = document.querySelector('[data-err="' + mode + '"]');
    err.textContent = "";
    var fd = new FormData();
    fd.append("mode", mode);
    fd.append("text", document.getElementById(mode + "-text").value);

    if (mode === "photo") {
      var photo = document.getElementById("photo-file").files[0];
      if (!photo) { err.textContent = "Нужно фото."; return; }
      fd.append("photo", photo);
      if (voice.mode === "preset") {
        if (!voice.id) { err.textContent = "Выберите голос."; return; }
        fd.append("voice_id", voice.id);
      } else if (voice.mode === "chat") {
        // Файл лежит на сервере — сюда ничего не кладём, там подхватят.
        if (!inbox.voice) {
          err.textContent = "Голосового ещё нет. Запишите его боту в чат.";
          return;
        }
      } else {
        var vf = document.getElementById("voice-file").files[0];
        if (!vf) { err.textContent = "Выберите файл с голосом."; return; }
        fd.append("voice", vf);
      }
    } else if (pickedVideo) {
      fd.append("video", pickedVideo);
    } else if (!inbox.video) {
      err.textContent = "Нужно видео: снимите на камеру, выберите файл "
                      + "или отправьте ролик боту в чат.";
      return;
    }

    var btn = document.querySelector('[data-submit="' + mode + '"]');
    btn.disabled = true;
    api("/api/avatar", { method: "POST", body: fd })
      .then(function (job) { btn.disabled = false; resume(job); })
      .catch(function (e) {
        btn.disabled = false;
        if (e.status === 409) { resume(e.data); return; }   // уже идёт — это прогресс
        err.textContent = e.message;
      });
  }

  // --- ожидание -------------------------------------------------------------

  function resume(job) {
    show("wait");
    render(job);
    clearInterval(poll);
    poll = setInterval(function () {
      api("/api/jobs/" + job.job_id).then(render).catch(function () {});
    }, 3000);
  }

  function render(job) {
    var bar = document.getElementById("wait-bar");
    var note = document.getElementById("wait-note");
    var title = document.getElementById("wait-title");

    if (job.status === "queued") {
      title.textContent = "В очереди";
      note.textContent = job.queue_position
        ? "Впереди задач: " + job.queue_position
        : "Скоро начну";
      bar.style.width = "4%";
      return;
    }
    if (job.status === "running") {
      title.textContent = "Делаю аватара";
      note.textContent = "Обычно это от тридцати секунд до полутора минут";
      bar.style.width = Math.max(6, job.progress) + "%";
      return;
    }

    clearInterval(poll);
    if (job.status === "done") {
      title.textContent = "Готово";
      note.textContent = "Ролик уже отправлен вам в чат";
      bar.style.width = "100%";
      if (job.media_url) {
        var v = document.getElementById("wait-video");
        v.src = job.media_url;
        v.hidden = false;
      }
    } else {
      title.textContent = "Не получилось";
      note.textContent = job.error || "Попробуйте ещё раз через пару минут";
      bar.style.width = "0";
    }
  }

  // --- старт ----------------------------------------------------------------

  document.querySelectorAll("[data-go]").forEach(function (b) {
    b.addEventListener("click", function () {
      var where = b.getAttribute("data-go");
      show(where);
      if (where === "video") refreshInbox();
    });
  });
  document.querySelectorAll("[data-submit]").forEach(function (b) {
    b.addEventListener("click", function () { submit(b.getAttribute("data-submit")); });
  });
  wireCounter("photo-text");
  wireCounter("video-text");
  wireVoice();
  wireInboxButtons();

  api("/api/state").then(function (state) {
    inbox = state.inbox || { voice: null, video: null };
    armedScreen = false;
    showInbox();
    if (state.job) { resume(state.job); return; }
    if (startAt === "photo" || startAt === "video") {
      show(startAt);
      if (startAt === "photo") {
        var tab = document.querySelector('[data-voice-tab="chat"]');
        if (tab) tab.click();
      }
      return;
    }
    show("pick");
  }).catch(function (e) {
    // Разделяем случаи: мост MAX не загрузился, подписи в окне нет, подпись
    // не принята сервером. Лечатся они по-разному, и без этой развилки на
    // три разные поломки видно одинаковое «не удалось».
    var why;
    if (!WA) {
      why = "Мост MAX не загрузился — подпись брать неоткуда. "
          + "Вне MAX это нормально, внутри — проверьте, что страница отдала max-web-app.js.";
    } else if (!INIT) {
      why = "MAX не передал подпись этому окну.";
    } else if (e.status === 401) {
      why = "MAX не принял подпись. Обычно это значит, что адрес приложения привязан "
          + "к другому боту, чем тот, чьим токеном она проверяется.";
    } else {
      why = e.message;
    }
    document.getElementById("boot").innerHTML =
      '<p class="err">' + why + "</p>"
      + '<button class="wide" id="boot-retry">Повторить</button>';
    var retry = document.getElementById("boot-retry");
    if (retry) retry.addEventListener("click", function () { location.reload(); });
  });
})();
