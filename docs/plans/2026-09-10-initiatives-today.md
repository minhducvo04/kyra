# Initiatives on Today and the HUD

- Add native source/receipt decoding and explicit accept/dismiss requests -> verify: a compiled Swift client against a real scratch server preserves the repeated acceptance receipt and dismissal reason.
- Add Today rows with reasons, exact evidence and optional dismissal feedback -> verify: simulator build and screenshot, separate preview bundle, no headset required.
- Add the same controls to the existing Tools panel -> verify: a real browser retains a row after a failed request, creates a reminder only on acceptance, dismisses with feedback, and escapes evidence text.
- Keep older servers usable -> verify: suggestions unavailable does not hide existing reminders or reviews; do not weaken transport security for a cloud URL.
- Check phone width and the generated digest visually -> verify: screenshots and no horizontal overflow.

This branch carries the reviewed daily prerequisites. Accepted suggestions are undated reminders, shown in Today and the reminder list. A client removes a suggestion only after a successful response; interrupted acceptance offers a retry. No client starts proposal generation or executes the proposed step.
