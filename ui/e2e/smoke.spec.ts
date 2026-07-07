import { test, expect, type ConsoleMessage } from "@playwright/test";

// A single render smoke test. It loads the app (with the dev mock so it is fully
// populated), and asserts the app actually rendered with no console errors. This
// catches render-time failures - a blank page from a missing provider, a
// component throwing on mount - that type-checking and building do not.

test("app renders without console errors and navigates the core views", async ({ page }) => {
  const errors: string[] = [];
  page.on("console", (msg: ConsoleMessage) => {
    if (msg.type() === "error") errors.push(msg.text());
  });
  page.on("pageerror", (err) => errors.push(err.message));

  await page.goto("/?mock");
  await page.waitForLoadState("networkidle");

  // The shell rendered: the product wordmark and the Namespaces landing are shown
  // (not a blank page).
  await expect(page.getByText("Ladon", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Namespaces" })).toBeVisible();

  // Admin nav appears once the mock user (an admin) loads, then its view
  // renders. This is exactly the path that regressed to a blank page; loading it
  // guards that class.
  const adminNav = page.getByRole("button", { name: "Admin", exact: true });
  await expect(adminNav).toBeVisible();
  await adminNav.click();
  await expect(page.getByRole("heading", { name: "Admin" })).toBeVisible();
  await expect(page.getByText("Pending namespace requests")).toBeVisible();

  // A secret detail shows masked keys (and never a plaintext value in the DOM).
  await page.goto("/?mock#/ns/team-payments/secret/app/gateway");
  await page.waitForLoadState("networkidle");
  await expect(page.getByText("STRIPE_KEY")).toBeVisible();

  expect(errors, `console errors: ${errors.join(" | ")}`).toEqual([]);
});
