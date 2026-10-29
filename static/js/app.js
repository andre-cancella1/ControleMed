
"use strict";

document.addEventListener("DOMContentLoaded", () => {
    const campoBusca = document.querySelector("#busca");

    if (campoBusca) {
        campoBusca.addEventListener("keydown", (evento) => {
            if (evento.key === "Escape") {
                campoBusca.value = "";
            }
        });
    }
});
