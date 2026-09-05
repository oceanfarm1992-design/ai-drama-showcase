async function loadManifest() {
  const statusEl = document.getElementById("status");
  const listEl = document.getElementById("series-list");

  try {
    const res = await fetch(MANIFEST_URL, { cache: "no-store" });
    if (!res.ok) throw new Error(`Manifest fetch failed: ${res.status}`);
    const manifest = await res.json();
    renderEpisodes(manifest.episodes || []);
    statusEl.remove();
  } catch (err) {
    statusEl.textContent = "Couldn't load episodes right now. Check back soon.";
    console.error(err);
  }
}

function renderEpisodes(episodes) {
  const listEl = document.getElementById("series-list");

  // Group by series_title, newest episode first within each series
  const bySeries = new Map();
  for (const ep of episodes) {
    if (!bySeries.has(ep.series_title)) bySeries.set(ep.series_title, []);
    bySeries.get(ep.series_title).push(ep);
  }

  for (const [seriesTitle, eps] of bySeries) {
    const block = document.createElement("section");
    block.className = "series-block";

    const seriesLength = eps[0]?.series_length ?? 30;
    const latestEp = Math.max(...eps.map((e) => e.episode_number));
    const isComplete = latestEp >= seriesLength;

    const heading = document.createElement("h2");
    heading.textContent = seriesTitle;
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = isComplete ? "COMPLETE" : `EP ${latestEp}/${seriesLength}`;
    heading.appendChild(badge);
    block.appendChild(heading);

    const grid = document.createElement("div");
    grid.className = "episode-grid";

    for (const ep of eps.sort((a, b) => b.episode_number - a.episode_number)) {
      grid.appendChild(renderCard(ep));
    }

    block.appendChild(grid);
    listEl.appendChild(block);
  }
}

function renderCard(ep) {
  const card = document.createElement("article");
  card.className = "episode-card";

  const video = document.createElement("video");
  video.src = ep.video_url;
  video.controls = true;
  video.preload = "none";

  const meta = document.createElement("div");
  meta.className = "episode-meta";
  meta.innerHTML = `
    <p class="ep-title">${escapeHtml(ep.title)}</p>
    <p class="ep-num">Episode ${ep.episode_number}/${ep.series_length} · ${escapeHtml(ep.language)}</p>
  `;

  card.appendChild(video);
  card.appendChild(meta);
  return card;
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

loadManifest();
