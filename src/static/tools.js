// Tools page: switches for whole toolsets and for single tools. Each change is saved at once.
document.querySelectorAll(".toolset").forEach(row => {
  const setSwitch = row.querySelector(".toolset-switch");
  const toolSwitches = [...row.querySelectorAll(".tool-switch")];
  const counter = row.querySelector(".n-on");

  async function save(input, kind, id) {
    input.disabled = true;
    try {
      const res = await api("/api/tools/switch", { kind, id, on: input.checked });
      document.getElementById("tools-on").textContent = res.tools_on;
    } catch (err) {
      input.checked = !input.checked;  // put the switch back: nothing was saved
      alert(err.message);
    } finally {
      input.disabled = kind === "tool" && !setSwitch.checked;
    }
  }

  setSwitch.addEventListener("change", async () => {
    await save(setSwitch, "toolset", row.dataset.id);
    row.classList.toggle("is-off", !setSwitch.checked);
    toolSwitches.forEach(t => { t.disabled = !setSwitch.checked; });
  });

  toolSwitches.forEach(t => t.addEventListener("change", async () => {
    await save(t, "tool", t.dataset.name);
    if (counter) counter.textContent = toolSwitches.filter(x => x.checked).length;
  }));
});
