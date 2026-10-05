# Extra page content (optional)

Put an HTML file here named after a page, and the build adds it as a new section at the bottom of
that page:

| File | Added to |
| --- | --- |
| `index.html` | Home |
| `schedule.html` | Schedule |
| `register.html` | Register |
| `rules.html` | Rules |
| `venue.html` | Venue |
| `sponsors.html` | Sponsors |
| `results.html` | Results |
| `faq.html` | FAQ |
| `terms.html` | Terms |

Example `content/venue.html`:

```html
<h2>After Party</h2>
<p>Join us after the awards at the pizza place across the street. All ages welcome.</p>
```

Use plain HTML. No `<script>`, `<style>`, or `style="…"` attributes; the site's security policy
blocks them, and the automatic check will fail. Add CSS to `assets/style.css` instead. This
README itself is ignored by the build.
