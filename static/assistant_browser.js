// assistant_browser.js — push-to-talk voice layer using Web Speech API
(function () {
  const btn      = document.getElementById('voice-btn');
  const statusEl = document.getElementById('voice-status');

  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    statusEl.textContent = 'Voice requires Chrome or Edge.';
    btn.disabled = true;
    btn.style.opacity = '0.4';
    return;
  }

  const recognition = new SpeechRecognition();
  recognition.lang = 'en-US';
  recognition.interimResults = false;
  recognition.maxAlternatives = 1;

  let listening = false;

  function startListening() {
    if (listening) return;
    try { recognition.start(); } catch (_) {}
  }

  btn.addEventListener('click', startListening);

  // Spacebar shortcut — skip if user is typing in an input
  document.addEventListener('keydown', e => {
    if (e.code === 'Space' && !e.target.matches('input, textarea, select')) {
      e.preventDefault();
      startListening();
    }
  });

  recognition.onstart = () => {
    listening = true;
    btn.classList.add('listening');
    statusEl.textContent = 'Listening…';
  };

  recognition.onend = () => {
    listening = false;
    btn.classList.remove('listening');
  };

  recognition.onresult = async (event) => {
    const text = event.results[0][0].transcript;
    statusEl.textContent = 'You: ' + text;

    try {
      const res = await fetch('/api/voice', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text })
      });
      const { reply, action } = await res.json();

      if (reply) {
        statusEl.textContent = 'Sprout: ' + reply;
        const utt = new SpeechSynthesisUtterance(reply);
        window.speechSynthesis.speak(utt);
      }

      // Apply board filter if Gemini returned one
      if (action && action.type === 'FILTER') {
        if (action.family === 'all') {
          setFamilyFilter('all');
        } else {
          setFamilyFilter(action.family);
          if (action.sub && action.sub !== 'all') setSubFilter(action.sub);
        }
      }

      // Refresh board after any task-modifying command
      loadTasks();

    } catch (err) {
      statusEl.textContent = 'Could not reach server.';
    }
  };

  recognition.onerror = (e) => {
    statusEl.textContent = e.error === 'not-allowed'
      ? 'Microphone blocked — allow access in browser settings.'
      : 'Mic error: ' + e.error;
    listening = false;
    btn.classList.remove('listening');
  };

  // Poll for reminders every 30 seconds and speak them aloud
  async function checkReminders() {
    try {
      const r = await fetch('/api/reminders/pending');
      const reminders = await r.json();
      reminders.forEach(rem => {
        const msg = 'Reminder: ' + rem.task;
        statusEl.textContent = msg;
        window.speechSynthesis.speak(new SpeechSynthesisUtterance(msg));
      });
    } catch (_) {}
  }
  setInterval(checkReminders, 30000);
})();
