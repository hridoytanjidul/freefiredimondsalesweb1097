(function () {
  "use strict";
  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };
  var csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";

  function store(kind) {
    try { return window[kind]; } catch (e) { return null; }
  }
  var ss = store("sessionStorage");

  /* ------------------------------------------------ flash close */
  $$(".flash .x").forEach(function (b) {
    b.addEventListener("click", function () { b.closest(".flash").remove(); });
  });

  /* ------------------------------------------------ account dropdown */
  var account = $(".account");
  if (account) {
    $(".account-btn", account).addEventListener("click", function (e) {
      e.stopPropagation();
      account.classList.toggle("open");
    });
    document.addEventListener("click", function () { account.classList.remove("open"); });
  }

  /* ------------------------------------------------ notice bar */
  var notice = $(".notice");
  if (notice) {
    if (ss && ss.getItem("noticeClosed") === "1") notice.remove();
    var nc = $(".notice-close", notice);
    if (nc) nc.addEventListener("click", function () {
      notice.remove();
      if (ss) ss.setItem("noticeClosed", "1");
    });
  }

  /* ------------------------------------------------ slider */
  var slider = $("[data-slider]");
  if (slider) {
    var slides = $$(".slide", slider);
    var dotsBox = $(".slider-dots");
    var idx = 0, timer = null;
    var go = function (n) {
      slides[idx].classList.remove("active");
      if (dotsBox) dotsBox.children[idx].classList.remove("active");
      idx = (n + slides.length) % slides.length;
      slides[idx].classList.add("active");
      if (dotsBox) dotsBox.children[idx].classList.add("active");
    };
    var start = function () { if (slides.length > 1) timer = setInterval(function () { go(idx + 1); }, 4500); };
    if (dotsBox && slides.length > 1) {
      slides.forEach(function (_, i) {
        var b = document.createElement("button");
        b.type = "button";
        b.setAttribute("aria-label", "Slide " + (i + 1));
        if (i === 0) b.className = "active";
        b.addEventListener("click", function () { clearInterval(timer); go(i); start(); });
        dotsBox.appendChild(b);
      });
    }
    // swipe
    var x0 = null;
    slider.addEventListener("touchstart", function (e) { x0 = e.touches[0].clientX; }, { passive: true });
    slider.addEventListener("touchend", function (e) {
      if (x0 === null) return;
      var dx = e.changedTouches[0].clientX - x0;
      if (Math.abs(dx) > 40) { clearInterval(timer); go(idx + (dx < 0 ? 1 : -1)); start(); }
      x0 = null;
    });
    start();
  }

  /* ------------------------------------------------ popup */
  var popup = $("#popup");
  if (popup && !(ss && ss.getItem("popupSeen") === "1")) {
    setTimeout(function () { popup.classList.add("open"); }, 700);
    var closePopup = function () {
      popup.classList.remove("open");
      if (ss) ss.setItem("popupSeen", "1");
    };
    $$("[data-close]", popup).forEach(function (b) { b.addEventListener("click", closePopup); });
    popup.addEventListener("click", function (e) { if (e.target === popup) closePopup(); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") closePopup(); });
  }

  /* ------------------------------------------------ recent orders (live) */
  var recent = $("#recent-list");
  if (recent) {
    var esc = function (s) {
      return String(s).replace(/[&<>"']/g, function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
      });
    };
    var labels = { pending: ["Pending", "warn"], processing: ["Processing", "info"], completed: ["Done", "ok"], cancelled: ["Cancelled", "bad"] };
    var load = function () {
      var btn = $("#recent-refresh");
      if (btn) btn.classList.add("spin");
      fetch("/api/recent-orders", { headers: { Accept: "application/json" } })
        .then(function (r) { return r.json(); })
        .then(function (rows) {
          if (!rows.length) { recent.innerHTML = '<li class="recent-empty">এখনো কোনো অর্ডার নেই</li>'; return; }
          recent.innerHTML = rows.map(function (o) {
            var st = labels[o.status] || [o.status, "info"];
            var av = o.avatar
              ? '<img class="avatar" src="' + esc(o.avatar) + '" alt="" referrerpolicy="no-referrer" loading="lazy">'
              : '<span class="avatar" style="--h:' + o.hue + '">' + esc(o.initial) + '</span>';
            return '<li>' + av +
              '<div class="who"><b>' + esc(o.name) + '</b><span>' + esc(o.item) + ' - ৳' + Number(o.price).toLocaleString("en-US") + '</span></div>' +
              '<span class="badge badge-' + st[1] + '">' + st[0] + '</span></li>';
          }).join("");
        })
        .catch(function () {})
        .then(function () { if (btn) btn.classList.remove("spin"); });
    };
    var rb = $("#recent-refresh");
    if (rb) rb.addEventListener("click", load);
    setInterval(function () { if (!document.hidden) load(); }, 30000);
  }

  /* ------------------------------------------------ countdowns (flash sale) */
  var timers = $$("[data-countdown]");
  if (timers.length) {
    var started = Date.now();
    var pad = function (n) { return (n < 10 ? "0" : "") + n; };
    var tick = function () {
      var elapsed = Math.floor((Date.now() - started) / 1000);
      timers.forEach(function (el) {
        var left = Math.max(0, parseInt(el.getAttribute("data-countdown"), 10) - elapsed);
        var d = Math.floor(left / 86400), h = Math.floor((left % 86400) / 3600), m = Math.floor((left % 3600) / 60), s = left % 60;
        var dEl = el.querySelector("[data-d]");
        if (dEl) { dEl.hidden = !d; dEl.textContent = d + "d"; }
        el.querySelector("[data-h]").textContent = pad(h);
        el.querySelector("[data-m]").textContent = pad(m);
        el.querySelector("[data-s]").textContent = pad(s);
        if (left === 0 && el.hasAttribute("data-reload") && !el._reloading) {
          el._reloading = true;   // sale started or ended: reload to show the new state
          setTimeout(function () { location.reload(); }, 1500);
        }
      });
    };
    tick();
    setInterval(tick, 1000);
  }

  /* ------------------------------------------------ spin wheel */
  var rotor = $("#wheel-rotor");
  if (rotor) {
    var spinBtns = [$("#spin-btn"), $("#spin-btn-2")].filter(Boolean);
    var resultBox = $("#spin-result");
    var statusBox = $("#spin-status");
    var segCount = $$("[data-prize-id]", rotor).length;
    var reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    var spinning = false, rotation = 0;
    var setStatus = function (text) {
      if (!statusBox) return;
      statusBox.innerHTML = '<b class="bn"></b>';
      statusBox.firstChild.textContent = text;
    };
    var showResult = function (d) {
      var lost = d.type === "nothing";
      $("#spin-emoji").textContent = lost ? "🍀" : "🎉";
      $("#spin-title").textContent = lost ? "Better luck next time!" : "You won " + d.label + "!";
      $("#spin-message").textContent = d.message;
      var codeBox = $("#spin-code");
      if (d.coupon_code) {
        $("#spin-code-text").textContent = d.coupon_code;
        $("#spin-copy").setAttribute("data-copy", d.coupon_code);
        codeBox.classList.remove("hide");
      } else {
        codeBox.classList.add("hide");
      }
      resultBox.classList.add("open");
    };
    var doSpin = function () {
      if (spinning) return;
      spinning = true;
      spinBtns.forEach(function (b) { b.disabled = true; });
      setStatus("ঘুরছে... 🎡");
      fetch("/api/spin", { method: "POST", headers: { "X-CSRF-Token": csrf, Accept: "application/json" } })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!d.ok) { spinning = false; setStatus(d.error || "স্পিন করা যায়নি।"); return; }
          // the server already chose the prize; turn the wheel so that segment stops under the pointer
          var seg = $('[data-prize-id="' + d.prize_id + '"]', rotor);
          var idx = seg ? parseInt(seg.getAttribute("data-index"), 10) : d.index;
          var step = 360 / Math.max(1, segCount);
          var target = (idx + 0.5) * step + (Math.random() - 0.5) * step * 0.6;
          var current = ((rotation % 360) + 360) % 360;
          var delta = (((360 - target - current) % 360) + 360) % 360;
          rotation += 360 * 6 + delta;
          rotor.style.transform = "rotate(" + rotation + "deg)";
          setTimeout(function () { showResult(d); }, reduceMotion ? 300 : 5400);
        })
        .catch(function () {
          spinning = false;
          setStatus("নেটওয়ার্ক সমস্যা — আবার চেষ্টা করুন।");
          spinBtns.forEach(function (b) { b.disabled = false; });
        });
    };
    spinBtns.forEach(function (b) { b.addEventListener("click", doSpin); });
    $$("[data-spin-close]").forEach(function (b) {
      b.addEventListener("click", function () { location.reload(); });   // refresh balance + cooldown
    });
  }

  /* ------------------------------------------------ copy buttons */
  $$("[data-copy]").forEach(function (b) {
    b.addEventListener("click", function () {
      var text = b.getAttribute("data-copy");
      var done = function () {
        var old = b.innerHTML;
        b.textContent = "Copied ✓";
        setTimeout(function () { b.innerHTML = old; }, 1400);
      };
      if (navigator.clipboard) navigator.clipboard.writeText(text).then(done, done);
      else { var t = document.createElement("textarea"); t.value = text; document.body.appendChild(t); t.select(); try { document.execCommand("copy"); } catch (e) {} t.remove(); done(); }
    });
  });

  /* ------------------------------------------------ amount chips (add money) */
  $$("[data-amount]").forEach(function (b) {
    b.addEventListener("click", function () {
      var input = $("#amount");
      if (input) { input.value = b.getAttribute("data-amount"); input.dispatchEvent(new Event("input")); }
    });
  });

  /* ------------------------------------------------ password show/hide */
  $$(".pw-toggle").forEach(function (b) {
    b.addEventListener("click", function () {
      var input = b.parentNode.querySelector("input");
      var show = input.type === "password";
      input.type = show ? "text" : "password";
      b.textContent = show ? "Hide" : "Show";
    });
  });

  /* ------------------------------------------------ payment form (product page + add money) */
  var form = $("#order-form") || $("#wallet-form");
  if (form) {
    var base = 0, discount = 0, couponOn = null;
    var needEl = $("#need-amount");
    var payAmountEls = $$(".pay-amount");
    var discLine = $("#discount-line"), discAmt = $("#discount-amount");
    var render = function () {
      var p = Math.max(0, base - discount);
      if (needEl) needEl.textContent = p;
      payAmountEls.forEach(function (el) { el.textContent = p ? "৳" + p : "—"; });
      if (discLine) { discLine.classList.toggle("hide", !discount); if (discAmt) discAmt.textContent = discount; }
    };

    /* coupon box: preview the discount; the server checks the code again at checkout */
    var couponInput = $("#coupon_code", form), couponMsg = $("#coupon-msg", form), couponBox = $(".coupon-box", form);
    var chips = $$(".coupon-chip", form);
    var couponNote = function (text, ok) {
      if (!couponMsg) return;
      couponMsg.textContent = text || "";
      couponMsg.className = "coupon-msg" + (text ? (ok ? " ok" : " bad") : "");
    };
    var clearCoupon = function () {
      discount = 0; couponOn = null;
      chips.forEach(function (c) { c.classList.remove("active"); });
      render();
    };
    var applyCoupon = function (code) {
      code = (code || "").replace(/\s/g, "").toUpperCase();
      if (!code) { clearCoupon(); couponNote(""); return; }
      if (couponBox && couponBox.getAttribute("data-logged-in") !== "1") { couponNote("কুপন ব্যবহার করতে আগে লগইন করুন।", false); return; }
      var pkg = $('input[name="package_id"]:checked', form);
      if (!pkg) { couponNote("আগে একটি প্যাকেজ সিলেক্ট করুন।", false); return; }
      var body = new FormData();
      body.append("code", code);
      body.append("package_id", pkg.value);
      couponNote("চেক করা হচ্ছে...", true);
      fetch("/api/coupon/check", { method: "POST", body: body, headers: { "X-CSRF-Token": csrf, Accept: "application/json" } })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d.ok) {
            discount = d.discount; couponOn = d.code;
            if (couponInput) couponInput.value = d.code;
            chips.forEach(function (c) { c.classList.toggle("active", c.getAttribute("data-coupon") === d.code); });
            couponNote("✓ " + d.code + " — ৳" + d.discount + " ছাড় পেয়েছেন!", true);
          } else {
            clearCoupon();
            couponNote(d.error || "কুপনটি ব্যবহার করা যাবে না।", false);
          }
          render();
        })
        .catch(function () { clearCoupon(); couponNote("এখন কুপন চেক করা যাচ্ছে না।", false); });
    };
    if (couponInput) {
      var applyBtn = $("#coupon-apply", form);
      if (applyBtn) applyBtn.addEventListener("click", function () { applyCoupon(couponInput.value); });
      couponInput.addEventListener("keydown", function (e) {
        if (e.key === "Enter") { e.preventDefault(); applyCoupon(couponInput.value); }
      });
      couponInput.addEventListener("input", function () {
        if (couponOn && couponInput.value.trim().toUpperCase() !== couponOn) { clearCoupon(); couponNote(""); }
      });
      chips.forEach(function (chip) {
        chip.addEventListener("click", function () {
          couponInput.value = chip.getAttribute("data-coupon");
          applyCoupon(couponInput.value);
        });
      });
    }

    var setPrice = function (p) {
      base = p;
      if (couponOn) applyCoupon(couponOn); else render();   // re-check the coupon for the new package
    };

    // packages
    $$('input[name="package_id"]', form).forEach(function (r) {
      r.addEventListener("change", function () { setPrice(parseInt(r.getAttribute("data-price"), 10) || 0); });
      if (r.checked) setPrice(parseInt(r.getAttribute("data-price"), 10) || 0);
    });
    var amountInput = $("#amount", form);
    if (amountInput) {
      amountInput.addEventListener("input", function () { setPrice(parseInt(amountInput.value, 10) || 0); });
      setPrice(parseInt(amountInput.value, 10) || 0);
    }

    // pay options -> show/hide manual box
    var manualBox = $("#manual-box", form);
    var syncPay = function () {
      var sel = $('input[name="pay_option"]:checked', form);
      var isManual = sel && sel.value === "manual";
      if (manualBox) {
        manualBox.classList.toggle("hide", !isManual);
        $$("input", manualBox).forEach(function (i) { if (i.type !== "radio") i.required = !!isManual; });
      }
      var walletWarn = $("#wallet-warn", form);
      if (walletWarn) walletWarn.classList.toggle("hide", !(sel && sel.value === "wallet"));
    };
    $$('input[name="pay_option"]', form).forEach(function (r) { r.addEventListener("change", syncPay); });
    syncPay();

    // manual method tabs -> show number
    var syncMethod = function () {
      var m = $('input[name="method_id"]:checked', form);
      $$(".pay-to", form).forEach(function (box) {
        box.classList.toggle("hide", !m || box.getAttribute("data-method") !== m.value);
      });
    };
    $$('input[name="method_id"]', form).forEach(function (r) { r.addEventListener("change", syncMethod); });
    syncMethod();

    form.addEventListener("submit", function (e) {
      var err = null;
      var pid = $("#player_id", form);
      var amt = $("#amount", form);
      if ($('input[name="package_id"]', form) && !$('input[name="package_id"]:checked', form)) err = "প্রথমে একটি প্যাকেজ সিলেক্ট করুন।";
      else if (pid && !pid.value.trim()) { err = "আপনার Player ID দিন।"; pid.focus(); }
      else if (amt && !(parseInt(amt.value, 10) > 0)) { err = "টাকার পরিমাণ দিন।"; amt.focus(); }
      else if (!$('input[name="pay_option"]:checked', form)) err = "পেমেন্ট অপশন সিলেক্ট করুন।";
      else if ($('input[name="pay_option"]:checked', form).value === "manual") {
        if (!$('input[name="method_id"]:checked', form)) err = "বিকাশ / নগদ / রকেট সিলেক্ট করুন।";
        else if (!$("#sender_number", form).value.trim()) err = "যে নম্বর থেকে টাকা পাঠিয়েছেন সেটি দিন।";
        else if (!$("#trx_id", form).value.trim()) err = "Transaction ID দিন।";
      }
      if (err) {
        e.preventDefault();
        alert(err);
        return;
      }
      var btn = $('button[type="submit"]', form);
      if (btn) { btn.classList.add("is-loading"); btn.textContent = "অপেক্ষা করুন..."; }
    });
  }

  /* ------------------------------------------------ Free Fire ID checker */
  $$("[data-check-id]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var input = $(btn.getAttribute("data-check-id"));
      var out = $(btn.getAttribute("data-result"));
      var uid = (input.value || "").replace(/\D/g, "");
      if (uid.length < 5) { out.className = "id-result bad"; out.textContent = "সঠিক Player ID দিন।"; return; }
      out.className = "id-result"; out.textContent = "চেক করা হচ্ছে...";
      btn.disabled = true;
      fetch("/api/check-id?uid=" + encodeURIComponent(uid), { headers: { Accept: "application/json" } })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d.ok) { out.className = "id-result ok"; out.textContent = "✓ Player Name: " + d.name; }
          else { out.className = "id-result bad"; out.textContent = d.error || "পাওয়া যায়নি"; }
        })
        .catch(function () { out.className = "id-result bad"; out.textContent = "এখন চেক করা যাচ্ছে না।"; })
        .then(function () { btn.disabled = false; });
    });
  });

  /* ------------------------------------------------ AI assistant */
  var aiEsc = function (s) {
    return s.replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; });
  };
  // runs on already-escaped text: links only to this site's pages ("/path", never "//host") or https:// pages
  var aiInline = function (s) {
    s = s.replace(/\[([^\]]+)\]\((\/(?![\/\\])[^\s)\\]*|https:\/\/[^\s)\\]+)\)/g, function (m, label, url) {
      var ext = url.indexOf("https://") === 0;
      return '<a href="' + url + '"' + (ext ? ' target="_blank" rel="noopener nofollow"' : "") + ">" + label + "</a>";
    });
    return s.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
  };
  var aiMarkdown = function (text) {
    var out = [], para = [], list = null;
    var flushPara = function () { if (para.length) { out.push("<p>" + para.join("<br>") + "</p>"); para = []; } };
    var flushList = function () { if (list) { out.push("<" + list.tag + ">" + list.items.join("") + "</" + list.tag + ">"); list = null; } };
    aiEsc(text).split("\n").forEach(function (raw) {
      var line = raw.trim(), m;
      if (!line) { flushPara(); flushList(); return; }
      if ((m = line.match(/^[-*•]\s+(.*)$/)) || (m = line.match(/^\d+[.)]\s+(.*)$/))) {
        var tag = /^\d/.test(line) ? "ol" : "ul";
        flushPara();
        if (!list || list.tag !== tag) { flushList(); list = { tag: tag, items: [] }; }
        list.items.push("<li>" + aiInline(m[1]) + "</li>");
        return;
      }
      flushList();
      para.push(aiInline(line.replace(/^#{1,4}\s+(.*)$/, "**$1**")));
    });
    flushPara(); flushList();
    return out.join("");
  };

  var aiPanel = $("#ai-panel");
  if (aiPanel) {
    var aiLog = $("#ai-log", aiPanel), aiForm = $("#ai-form", aiPanel), aiInput = $("#ai-input", aiPanel);
    var aiSend = $(".ai-send", aiPanel), aiWelcome = $(".ai-welcome", aiPanel);
    var AI_KEY = "ai_chat_v1", aiBusy = false, aiHist = [];
    var aiUser = aiPanel.getAttribute("data-uid") || "0";
    try {
      var saved = JSON.parse((ss && ss.getItem(AI_KEY)) || "{}");
      if (saved && saved.uid === aiUser && Array.isArray(saved.messages)) aiHist = saved.messages;  // another login: start fresh
    } catch (e) { aiHist = []; }
    var aiSave = function () {
      aiHist = aiHist.slice(-20);
      try { if (ss) ss.setItem(AI_KEY, JSON.stringify({ uid: aiUser, messages: aiHist })); } catch (e) { /* private mode */ }
    };
    var aiScroll = function () { aiLog.scrollTop = aiLog.scrollHeight; };
    var aiBubble = function (role, text, isError) {
      var d = document.createElement("div");
      d.className = "ai-msg " + (role === "user" ? "ai-user" : "ai-bot") + (isError ? " ai-error" : "");
      if (role === "user") d.textContent = text; else d.innerHTML = aiMarkdown(text);
      aiLog.appendChild(d);
      aiScroll();
      return d;
    };
    aiHist.forEach(function (m) { aiBubble(m.role, m.content); });

    var aiOpen = function () {
      aiPanel.hidden = false;
      document.body.classList.add("ai-open");
      setTimeout(function () { aiScroll(); aiInput.focus(); }, 30);
    };
    var aiClose = function () { aiPanel.hidden = true; document.body.classList.remove("ai-open"); };
    $$("[data-ai-open]").forEach(function (b) { b.addEventListener("click", aiOpen); });
    $$("[data-ai-close]", aiPanel).forEach(function (b) { b.addEventListener("click", aiClose); });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !aiPanel.hidden && !aiPanel.classList.contains("ai-page")) aiClose();
    });
    var aiGrow = function () { aiInput.style.height = "auto"; aiInput.style.height = Math.min(aiInput.scrollHeight, 120) + "px"; };

    var aiAsk = function (text) {
      text = (text || "").trim();
      if (!text || aiBusy) return;
      if (text.length > 1000) { aiBubble("bot", "প্রশ্নটি অনেক বড় (সর্বোচ্চ ১০০০ অক্ষর)।", true); return; }
      aiBusy = true; aiSend.disabled = true;
      aiInput.value = ""; aiGrow();
      aiHist.push({ role: "user", content: text });
      aiBubble("user", text);
      var bot = aiBubble("bot", "");
      bot.innerHTML = '<span class="ai-typing" aria-label="typing"><i></i><i></i><i></i></span>';
      var answer = "";
      var show = function () { bot.innerHTML = aiMarkdown(answer); aiScroll(); };
      var fail = function (msg) {   // forget the unanswered question so the chat stays consistent
        if (aiHist.length && aiHist[aiHist.length - 1].role === "user") aiHist.pop();
        aiSave();
        bot.classList.add("ai-error");
        bot.textContent = msg;
        aiScroll();
      };
      fetch("/api/ai/chat", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf, Accept: "text/plain" },
        body: JSON.stringify({ messages: aiHist.slice(-12) })
      }).then(function (r) {
        if (!r.ok) {
          return r.json().catch(function () { return {}; }).then(function (d) {
            throw new Error(d.error || (r.status === 400 ? "পেজটি রিলোড করে আবার চেষ্টা করুন।" : "দুঃখিত, এখন উত্তর দেওয়া যাচ্ছে না।"));
          });
        }
        if (!r.body || !r.body.getReader || !window.TextDecoder) return r.text().then(function (t) { answer = t; show(); });
        var reader = r.body.getReader(), dec = new TextDecoder();
        var pump = function () {
          return reader.read().then(function (res) {
            if (res.done) { answer += dec.decode(); show(); return; }
            answer += dec.decode(res.value, { stream: true });
            show();
            return pump();
          });
        };
        return pump();
      }).then(function () {
        if (!answer.trim()) { fail("দুঃখিত, এখন উত্তর দেওয়া যাচ্ছে না।"); return; }
        aiHist.push({ role: "assistant", content: answer });
        aiSave();
      }).catch(function (err) {
        fail(err && err.message && err.message.indexOf("Failed to fetch") < 0 ? err.message : "নেটওয়ার্ক সমস্যা। আবার চেষ্টা করুন।");
      }).then(function () {
        aiBusy = false; aiSend.disabled = false;
        aiInput.focus();
      });
    };

    $$("[data-ai-ask]", aiPanel).forEach(function (b) {
      b.addEventListener("click", function () { aiAsk(b.getAttribute("data-ai-ask")); });
    });
    $$("[data-ai-reset]", aiPanel).forEach(function (b) {
      b.addEventListener("click", function () {
        if (aiBusy) return;
        aiHist = []; aiSave();
        $$(".ai-msg", aiLog).forEach(function (m) { if (m !== aiWelcome) m.parentNode.removeChild(m); });
        aiInput.focus();
      });
    });
    aiInput.addEventListener("input", aiGrow);
    aiInput.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); aiAsk(aiInput.value); }
    });
    aiForm.addEventListener("submit", function (e) { e.preventDefault(); aiAsk(aiInput.value); });
    if (aiPanel.classList.contains("ai-page")) aiScroll();
  }

  /* ------------------------------------------------ confirm buttons */
  $$("[data-confirm]").forEach(function (el) {
    el.addEventListener("click", function (e) {
      if (!confirm(el.getAttribute("data-confirm"))) e.preventDefault();
    });
  });

  void csrf;
})();
