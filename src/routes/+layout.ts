// Keys live in localStorage and every request is made from the browser, so there
// is nothing for a server render to do. Prerendering the shell also means the
// Python function never gets woken up just to serve the page.
export const ssr = false;
export const prerender = true;
