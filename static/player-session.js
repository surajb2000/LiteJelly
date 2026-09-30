/* Playback lifetime, request cancellation, and compatibility state accessors. */
(function () {
  'use strict';

  function PlayerSession() {
    this.generation = 0;
    this.active = false;
    this.videoId = '';
    this.operations = {};
    this.values = {};
    this.target = null;
  }

  /** Keep existing view helpers attached to one owner of playback state. */
  PlayerSession.prototype.bind = function (state, names) {
    const session = this;
    names.forEach(name => {
      session.values[name] = state[name];
      Object.defineProperty(state, name, {
        enumerable: true,
        get: () => session.values[name],
        set: value => { session.values[name] = value; }
      });
    });
  };

  /** Start a new title, invalidating every operation from the previous title. */
  PlayerSession.prototype.begin = function (videoId) {
    this.end();
    this.active = true;
    this.videoId = videoId;
    this.values.restartToken = 0;
  };

  /** Invalidate one operation category, even on browsers without AbortController. */
  PlayerSession.prototype.cancel = function (kind) {
    const previous = this.operations[kind];
    if (previous) {
      if (previous.controller) previous.controller.abort();
      previous.cleanups.forEach(cleanup => cleanup());
    }
    delete this.operations[kind];
  };

  /** Replace a pending operation and return its lifetime ticket and optional signal. */
  PlayerSession.prototype.next = function (kind) {
    this.cancel(kind);
    const controller = window.AbortController ? new window.AbortController() : null;
    const ticket = { kind: kind, generation: this.generation, controller: controller,
      signal: controller ? controller.signal : undefined, cleanups: [] };
    this.operations[kind] = ticket;
    return ticket;
  };

  /** True only while the title and operation that requested work still own the result. */
  PlayerSession.prototype.current = function (ticket) {
    return !!ticket && this.active && ticket.generation === this.generation &&
      this.operations[ticket.kind] === ticket;
  };

  /** Report whether a plan is still being negotiated for the current title. */
  PlayerSession.prototype.planning = function () {
    const operation = this.operations.plan;
    return this.current(operation) && !operation.finished;
  };

  /** Attach a one-shot media listener which cancellation also removes. */
  PlayerSession.prototype.once = function (ticket, target, event, callback) {
    const session = this;
    const listener = function () {
      target.removeEventListener(event, listener);
      if (session.current(ticket)) callback();
    };
    target.addEventListener(event, listener);
    ticket.cleanups.push(() => target.removeEventListener(event, listener));
  };

  /** End playback and release pending requests, callbacks and scheduled player work. */
  PlayerSession.prototype.end = function () {
    this.active = false;
    this.generation++;
    Object.keys(this.operations).forEach(kind => this.cancel(kind));
    ['seekTimer', 'osdTimer', 'subtitleTimer', 'audioApplyTimer', 'trickplayTimer',
      'upNextTimer', 'skipRetry'].forEach(name => {
      clearTimeout(this.values[name]);
      this.values[name] = null;
    });
    this.values.playback = null;
    this.values.offset = 0;
    this.values.restartAt = null;
    this.values.pendingSeek = null;
    this.values.scrubbing = false;
    this.target = null;
  };

  window.LiteJellyPlayerSession = PlayerSession;
})();
