/* Touch feedback: play a short click sound on tap for every tappable
 * element. See docs/superpowers/specs/2026-09-30-touch-feedback-design.md
 * §4.3. The TAPPABLE selector below must match, token for token, the
 * selector list used in web_interface/tailwind.input.css for the :active
 * press-state CSS (Task 3).
 *
 * ES5 syntax, no dependencies -- vendored alongside htmx for the offline
 * tablet dashboard.
 */
(function () {
  "use strict";

  // iOS Safari only applies :active styling to an element when a
  // touchstart listener exists somewhere on the page.
  document.addEventListener(
    "touchstart",
    function () {},
    { passive: true }
  );

  var TAPPABLE =
    'button, [type="submit"], [type="button"], a[href], label[for], summary, input:not([type="hidden"]), select, textarea';

  document.addEventListener(
    "pointerdown",
    function (event) {
      var audio = document.getElementById("tap-sound");
      if (!audio) {
        return;
      }
      if (!(event.target instanceof Element) || !event.target.closest(TAPPABLE)) {
        return;
      }
      audio.currentTime = 0;
      var p = audio.play();
      if (p && p.catch) {
        p.catch(function () {});
      }
    },
    { passive: true }
  );
})();
