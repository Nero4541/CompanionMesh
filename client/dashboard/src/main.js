import { mount } from "svelte";
import App from "./App.svelte";
import { takeTokenFromUrl } from "./lib/api.js";
import "./app.css";

takeTokenFromUrl();

export default mount(App, { target: document.getElementById("app") });
