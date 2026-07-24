# Two-hop fan: acts decomposing into recitals & articles, articles further into (sub-)articles. Run from repo root; writes figure-decomposition.png
library(tidyverse)
library(arrow)

surface <- "#fcfcfb"
ink <- "#0b0b0b"
ink2 <- "#52514e"
muted <- "#898781"
pal <- c(
  "GDPR" = "#56B4E9", "AI Act" = "#E69F00",
  "DSA" = "#009E73", "REACH" = "#CC79A7"
)

acts <- tribble(
  ~celex_id, ~act,
  "32016R0679", "GDPR",
  "32024R1689", "AI Act",
  "32022R2065", "DSA",
  "32006R1907", "REACH"
)

units <- read_parquet("output/full-run/text_units.parquet") |>
  inner_join(acts, by = "celex_id") |>
  filter(type %in% c("recital", "article")) |>
  group_by(act) |>
  summarise(
    recitals = sum(type == "recital"),
    articles = n_distinct(number[type == "article"]),
    subarticles = sum(type == "article")
  ) |>
  arrange(match(act, acts$act))

# Column x-extents: act box, recitals/articles blocks, (sub-)article block.
x1a <- 0.10; x1b <- 0.14
x2a <- 0.46; x2b <- 0.50
x3a <- 0.82; x3b <- 0.84

# Vertical layout per act on a shared row-count scale.
g2 <- 50; act_gap <- 150; act_box <- 90
rows <- list(); offset <- 0
for (i in seq_len(nrow(units))) {
  u <- units[i, ]
  r_top <- offset; r_bot <- r_top + u$recitals
  a_top <- r_bot + g2; a_bot <- a_top + u$articles
  col2_mid <- (r_top + a_bot) / 2
  s_mid <- (a_top + a_bot) / 2
  s_top <- s_mid - u$subarticles / 2; s_bot <- s_mid + u$subarticles / 2
  rows[[i]] <- tibble(
    act = u$act, recitals = u$recitals, articles = u$articles,
    subarticles = u$subarticles,
    act_top = col2_mid - act_box / 2, act_bot = col2_mid + act_box / 2,
    act_mid = col2_mid,
    r_top = r_top, r_bot = r_bot, a_top = a_top, a_bot = a_bot,
    s_top = s_top, s_bot = s_bot, s_mid = s_mid
  )
  offset <- max(a_bot, s_bot) + act_gap
}
lay <- bind_rows(rows)

# Ribbon polygon between two vertical edges using smoothstep interpolation.
ribbon_poly <- function(xf, xt, y_from_top, y_from_bot, y_to_top, y_to_bot, act) {
  t <- seq(0, 1, length.out = 120)
  s <- t^2 * (3 - 2 * t)
  x <- xf + t * (xt - xf)
  bind_rows(
    tibble(x = x, y = y_from_top + s * (y_to_top - y_from_top)),
    tibble(x = rev(x), y = rev(y_from_bot + s * (y_to_bot - y_from_bot)))
  ) |> mutate(act = act, id = paste(act, xf, y_to_top))
}
ribbons <- pmap_dfr(
  list(lay$act, lay$act_top, lay$act_mid, lay$act_bot, lay$r_top, lay$r_bot,
       lay$a_top, lay$a_bot, lay$s_top, lay$s_bot),
  function(act, at, am, ab, rt, rb, a2t, a2b, st, sb) {
    bind_rows(
      ribbon_poly(x1b, x2a, at, am, rt, rb, act),
      ribbon_poly(x1b, x2a, am, ab, a2t, a2b, act),
      ribbon_poly(x2b, x3a, a2t, a2b, st, sb, act)
    )
  }
)

fmt <- scales::label_comma()
p <- ggplot() +
  geom_polygon(data = ribbons, aes(x, y, group = id, fill = act), alpha = 0.55, colour = NA) +
  geom_rect(data = lay, aes(xmin = x1a, xmax = x1b, ymin = act_top, ymax = act_bot, fill = act), colour = NA) +
  geom_rect(data = lay, aes(xmin = x2a, xmax = x2b, ymin = r_top, ymax = r_bot, fill = act), colour = NA) +
  geom_rect(data = lay, aes(xmin = x2a, xmax = x2b, ymin = a_top, ymax = a_bot, fill = act), colour = NA) +
  geom_rect(data = lay, aes(xmin = x3a, xmax = x3b, ymin = s_top, ymax = s_bot, fill = act), colour = NA) +
  geom_text(
    data = lay, aes(x = x1a - 0.015, y = act_mid, label = act),
    hjust = 1, family = "Crimson Text", colour = ink, size = 4.8, fontface = "bold"
  ) +
  geom_text(
    data = lay, aes(x = x2b + 0.012, y = (r_top + r_bot) / 2, label = paste(recitals, "recitals")),
    hjust = 0, family = "Crimson Text", colour = ink2, size = 3.5
  ) +
  geom_text(
    data = lay, aes(x = x2a - 0.012, y = (a_top + a_bot) / 2, label = paste(articles, "articles")),
    hjust = 1, family = "Crimson Text", colour = ink2, size = 3.5
  ) +
  geom_text(
    data = lay, aes(x = x3b + 0.012, y = s_mid, label = paste(fmt(subarticles), "(sub-)articles")),
    hjust = 0, family = "Crimson Text", colour = ink2, size = 3.7
  ) +
  annotate("text",
    x = c(x1a, x2a, x3a - 0.10), y = -120,
    label = c("One act…", "…recitals & articles…", "…and every (sub-)article"),
    hjust = 0, family = "Crimson Text", colour = ink2, size = 4.1
  ) +
  scale_fill_manual(values = pal, guide = "none") +
  scale_y_reverse() +
  xlim(0, 1.02) +
  labs(
    title = "EU law, decomposed",
    subtitle = "Landmark regulations split into recitals and (sub-)articles — eurlex-builder turns EUR-Lex documents\ninto granular, individually addressable text units at configurable resolution",
    caption = "Data: EUR-Lex / Cellar, Publications Office of the EU · github.com/tseidl/eurlex-builder"
  ) +
  theme_void(base_family = "Crimson Text") +
  theme(
    plot.background = element_rect(fill = surface, colour = NA),
    plot.title = element_text(colour = ink, face = "bold", size = 17),
    plot.subtitle = element_text(colour = ink2, size = 10.5, lineheight = 1.15, margin = margin(t = 4, b = 12)),
    plot.caption = element_text(colour = muted, size = 8, hjust = 0, margin = margin(t = 10)),
    plot.margin = margin(16, 24, 12, 24)
  )

ggsave(
  "figure-decomposition.png",
  p,
  width = 9, height = 7.8, dpi = 300, device = ragg::agg_png, bg = surface
)
