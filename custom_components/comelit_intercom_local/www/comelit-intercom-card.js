/**
 * Comelit Intercom Card — Play-to-start intercom camera card.
 *
 * Shows the camera snapshot with a play button overlay. Clicking play
 * starts the video session. Navigating away stops it automatically.
 *
 * Install:
 *   The Lovelace resource is registered automatically on HA startup.
 *
 *   Add card to dashboard (YAML):
 *      type: custom:comelit-intercom-card
 *      camera_entity: camera.comelit_intercom_live_feed
 *      start_entity: button.comelit_intercom_start_video   # optional
 *      stop_entity:  button.comelit_intercom_stop_video_feed
 */
class ComelitIntercomCard extends HTMLElement {
  constructor() {
    super();
    this._hass = null;
    this._config = null;
    this._playing = false;
    this._liveCard = null;
    this._onLocationChanged = null;
    this.attachShadow({ mode: "open" });
  }

  // -------------------------------------------------------------------------
  // Lovelace lifecycle
  // -------------------------------------------------------------------------

  setConfig(config) {
    if (!config.camera_entity) {
      throw new Error("Missing required config: camera_entity");
    }
    this._config = config;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (this._liveCard) this._liveCard.hass = hass;
    if (!this._playing) this._refreshThumbnail();
  }

  connectedCallback() {
    // Listen for HA navigation to stop video when user leaves the view.
    // Many Lovelace panel types hide views with CSS instead of removing
    // elements from the DOM, so disconnectedCallback alone is not enough.
    this._onLocationChanged = () => {
      setTimeout(() => {
        if (!this.isConnected || !this._isVisible()) {
          this._stopVideo();
        }
      }, 0);
    };
    window.addEventListener("location-changed", this._onLocationChanged);
  }

  disconnectedCallback() {
    window.removeEventListener("location-changed", this._onLocationChanged);
    this._onLocationChanged = null;
    this._stopVideo();
  }

  getCardSize() {
    return 4;
  }

  static getStubConfig() {
    return {
      camera_entity: "camera.comelit_intercom_live_feed",
      start_entity: "button.comelit_intercom_start_video",
      stop_entity: "button.comelit_intercom_stop_video_feed",
    };
  }

  // -------------------------------------------------------------------------
  // Rendering
  // -------------------------------------------------------------------------

  _render() {
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        ha-card { overflow: hidden; }

        /* Idle view — thumbnail + play button */
        .idle {
          position: relative;
          background: #000;
          /* Maintain 800×480 (5:3) aspect ratio */
          aspect-ratio: 5 / 3;
          width: 100%;
          cursor: pointer;
        }
        .thumbnail {
          width: 100%;
          height: 100%;
          object-fit: cover;
          display: block;
        }
        .play-btn {
          position: absolute;
          inset: 0;
          display: flex;
          align-items: center;
          justify-content: center;
        }
        .play-circle {
          width: 72px;
          height: 72px;
          border-radius: 50%;
          background: rgba(0, 0, 0, 0.55);
          border: 2.5px solid rgba(255, 255, 255, 0.85);
          display: flex;
          align-items: center;
          justify-content: center;
          transition: background 0.15s, transform 0.15s;
        }
        .idle:hover .play-circle {
          background: rgba(0, 0, 0, 0.8);
          transform: scale(1.08);
        }
        .play-circle svg {
          fill: #fff;
          width: 34px;
          height: 34px;
          margin-left: 5px; /* optical centering for play triangle */
        }

        /* Live view — stream + stop button */
        .live { display: none; position: relative; }
        .stop-btn {
          position: absolute;
          top: 8px;
          right: 8px;
          width: 32px;
          height: 32px;
          border-radius: 50%;
          background: rgba(0, 0, 0, 0.55);
          border: none;
          cursor: pointer;
          display: flex;
          align-items: center;
          justify-content: center;
          z-index: 10;
          transition: background 0.15s;
        }
        .stop-btn:hover { background: rgba(180, 0, 0, 0.75); }
        .stop-btn svg { fill: #fff; width: 16px; height: 16px; }
      </style>

      <ha-card>
        <!-- Idle state: snapshot + play button -->
        <div class="idle" id="idle">
          <img class="thumbnail" id="thumbnail" />
          <div class="play-btn">
            <div class="play-circle">
              <!-- Material play icon -->
              <svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
            </div>
          </div>
        </div>

        <!-- Live state: stream card + stop button -->
        <div class="live" id="live">
          <button class="stop-btn" id="stop-btn" title="Stop video">
            <!-- Material stop icon -->
            <svg viewBox="0 0 24 24"><path d="M6 6h12v12H6z"/></svg>
          </button>
          <div id="stream-slot"></div>
        </div>
      </ha-card>
    `;

    this.shadowRoot.getElementById("idle").addEventListener("click", () => {
      this._startVideo();
    });
    this.shadowRoot.getElementById("stop-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      this._stopVideo();
      this._pressStop();
    });
  }

  // -------------------------------------------------------------------------
  // Video state
  // -------------------------------------------------------------------------

  async _startVideo() {
    if (this._playing || !this._hass || !this._config) return;
    this._playing = true;

    if (this._config.start_entity) {
      this._hass.callService("button", "press", {
        entity_id: this._config.start_entity,
      });
    }

    // Build the inner live card using HA helpers so the element is fully
    // upgraded before setConfig is called (document.createElement alone
    // returns an unupgraded element without setConfig).
    const helpers = await window.loadCardHelpers();
    this._liveCard = await helpers.createCardElement({
      type: "picture-entity",
      entity: this._config.camera_entity,
      camera_view: "live",
      show_name: false,
      show_state: false,
    });
    this._liveCard.hass = this._hass;
    this.shadowRoot.getElementById("stream-slot").appendChild(this._liveCard);

    this.shadowRoot.getElementById("idle").style.display = "none";
    this.shadowRoot.getElementById("live").style.display = "block";
  }

  _stopVideo() {
    if (!this._playing) return;
    this._playing = false;

    // Tear down the live card
    const slot = this.shadowRoot.getElementById("stream-slot");
    if (slot) slot.innerHTML = "";
    this._liveCard = null;

    this.shadowRoot.getElementById("idle").style.display = "";
    this.shadowRoot.getElementById("live").style.display = "none";
    this._refreshThumbnail();
  }

  _pressStop() {
    if (this._hass && this._config && this._config.stop_entity) {
      this._hass.callService("button", "press", {
        entity_id: this._config.stop_entity,
      });
    }
  }

  // -------------------------------------------------------------------------
  // Helpers
  // -------------------------------------------------------------------------

  _refreshThumbnail() {
    if (!this._hass || !this._config) return;
    const state = this._hass.states[this._config.camera_entity];
    const token = state?.attributes?.access_token;
    const img = this.shadowRoot.getElementById("thumbnail");
    if (img && token) {
      img.src = `/api/camera_proxy/${this._config.camera_entity}?token=${token}&t=${Date.now()}`;
    }
  }

  _isVisible() {
    // getBoundingClientRect() returns zero dimensions for elements hidden
    // anywhere in their ancestor chain (including across shadow DOM).
    const rect = this.getBoundingClientRect();
    return rect.width > 0 || rect.height > 0;
  }
}

customElements.define("comelit-intercom-card", ComelitIntercomCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "comelit-intercom-card",
  name: "Comelit Intercom Camera",
  description: "Intercom camera with play button — click to start, auto-stops on navigation.",
});
