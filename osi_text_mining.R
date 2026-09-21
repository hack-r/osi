# File:   osi_text_mining.R
# Author: Peikoff, Ason
# Notes:  Manual review of HITL AI data scraping of OSI articles.

# Libraries & Options -----------------------------------------------------

# sudo dnf install -y libuv-devel (for Fedora)

# Set working directory
setwd("/home/user/osi")

# Linux Binary Repo? (or use micromamba)
options(repos = c(
  CRAN = "https://packagemanager.posit.co/cran/latest"
))


# Libraries
if (!require("pacman"))  install.packages("pacman"); require(pacman)

p_load(
  #arrow,
  DBI,
  digest,
  dplyr,
  #flextable,
  forcats,
  #fs,
  ggplot2,
  ggrepel,
  htmlwidgets,
  janitor,
  jsonlite,
  knitr,
  purrr,
  #quanteda,
  #quanteda.textplots,
  #quanteda.textstats,
  RSQLite,
  readr,
  sentimentr,
  scales,
  stringr,
  stm,
  systemfonts,
  tibble,
  tidytext,
  tidyr,
  tm,
  wordcloud2
)

# Options
TEXT_DIR       <- "texts"
D1_SQLITE      <- "/home/user/osi/osi_citations.sqlite"
CITATION_TABLE <- "articles"

DATA_DIR       <- "data"
FIGURE_DIR     <- "figures"

MIN_DOC_FREQ   <- 3
MAX_DOC_FREQ   <- 0.995

NGRAM_MAX      <- 3

SKIPGRAM_MAX_N    <- 2
SKIPGRAM_MAX_SKIP <- 2

N_TOP_TERMS       <- 10 #15
N_WORDCLOUD_TERMS <- 75

#STM_K             <- 6 # set it by the modeling step

# Create Output Directories -----------------------------------------------
dir.create(DATA_DIR)
dir.create(FIGURE_DIR)

dir.create(paste0(DATA_DIR, "/tfidf"))
dir.create(paste0(DATA_DIR, "/topics"))
dir.create(paste0(DATA_DIR, "/sentiment"))
dir.create(paste0(DATA_DIR, "/frequency"))
dir.create(paste0(DATA_DIR, "/keyness"))
dir.create(paste0(DATA_DIR, "/wordclouds"))
dir.create(paste0(DATA_DIR, "/comparison"))


# Import Raw Data ---------------------------------------------------------

# Loader
read_text_safely <- function(filename) {
  tryCatch(
    readr::read_file(filename),
    error = function(e) {
      warning("Could not read: ", filename)
      ""
    }
  )
}

# Parsers
clean_text <- function(x) {
  x |>
    stringr::str_replace_all("\r\n?", "\n") |>
    stringr::str_replace_all("[ \t]+", " ") |>
    stringr::str_replace_all("\n{3,}", "\n\n") |>
    stringr::str_trim()
}


safe_filename <- function(x) {
  x |>
    stringr::str_replace_all("[^A-Za-z0-9_-]+", "_") |>
    stringr::str_sub(1, 120)
}


parse_header_field <- function(header_text, field_name) {
  escaped_field_name <- stringr::str_escape(field_name)
  
  pattern <- paste0(
    "(?m)^",
    escaped_field_name,
    "\\s*:\\s*(.*?)\\s*$"
  )
  
  match <- stringr::str_match(header_text, pattern)
  
  if (is.na(match[1, 2])) {
    return(NA_character_)
  }
  
  stringr::str_trim(match[1, 2])
}


parse_full_text_record <- function(filename) {
  raw_text <- read_text_safely(filename)
  
  # A valid record should have two long lines of "=":
  #
  #   first line:  beginning of structured header
  #   second line: end of structured header
  #
  # Everything after the second delimiter is article body text.
  
  delimiter_matches <- stringr::str_locate_all(
    raw_text,
    "(?m)^={10,}[[:space:]]*$"
  )[[1]]
  
  if (nrow(delimiter_matches) < 2) {
    warning(
      "Fewer than two header delimiters found in: ",
      filename
    )
    
    header_text <- raw_text
    body_text   <- ""
  } else {
    first_delimiter_end  <- delimiter_matches[1, 2]
    second_delimiter_end <- delimiter_matches[2, 2]
    
    header_text <- stringr::str_sub(
      raw_text,
      first_delimiter_end + 1,
      second_delimiter_matches <- second_delimiter_end
    )
    
    body_text <- stringr::str_sub(
      raw_text,
      second_delimiter_end + 1
    )
  }
  
  filename_stem <- fs::path_ext_remove(fs::path_file(filename))
  
  published_raw <- parse_header_field(
    header_text,
    "Published"
  )
  
  published_date <- suppressWarnings(
    as.POSIXct(
      published_raw,
      format = "%Y-%m-%dT%H:%M:%OS",
      tz = "UTC"
    )
  )
  
  published_year <- suppressWarnings(
    as.integer(
      stringr::str_extract(
        dplyr::coalesce(published_raw, ""),
        "\\b(?:18|19|20|21)[0-9]{2}\\b"
      )
    )
  )
  
  filename_year <- suppressWarnings(
    as.integer(
      stringr::str_extract(
        filename_stem,
        "\\b(?:18|19|20|21)[0-9]{2}\\b"
      )
    )
  )
  
  year <- dplyr::coalesce(
    published_year,
    filename_year
  )
  
  title <- parse_header_field(
    header_text,
    "Title"
  )
  
  subtitle <- parse_header_field(
    header_text,
    "Subtitle"
  )
  
  authors_raw <- parse_header_field(
    header_text,
    "Author(s)"
  )
  
  authors <- if (is.na(authors_raw)) {
    NA_character_
  } else {
    authors_raw |>
      stringr::str_split("\\s*;\\s*") |>
      purrr::pluck(1) |>
      paste(collapse = "; ")
  }
  
  item_key <- parse_header_field(
    header_text,
    "Item key"
  )
  
  tibble(
    path              = filename,
    filename          = fs::path_file(filename),
    filename_stem     = filename_stem,
    item_key          = item_key,
    title             = title,
    subtitle          = subtitle,
    author_raw        = authors_raw,
    author            = authors,
    published_raw     = published_raw,
    published_date    = published_date,
    year              = year,
    publication       = parse_header_field(
      header_text,
      "Publication"
    ),
    section           = parse_header_field(
      header_text,
      "Section"
    ),
    item_type         = parse_header_field(
      header_text,
      "Item type"
    ),
    access            = parse_header_field(
      header_text,
      "Access"
    ),
    url               = parse_header_field(
      header_text,
      "URL"
    ),
    source_word_count = suppressWarnings(
      as.integer(
        parse_header_field(
          header_text,
          "Word count \\(source\\)"
        )
      )
    ),
    retrieved         = parse_header_field(
      header_text,
      "Retrieved"
    ),
    text_status       = parse_header_field(
      header_text,
      "Text status"
    ),
    text_strategy     = parse_header_field(
      header_text,
      "Text strategy"
    ),
    text_words        = suppressWarnings(
      as.integer(
        parse_header_field(
          header_text,
          "Text words"
        )
      )
    ),
    text_sha256       = parse_header_field(
      header_text,
      "Text sha256"
    ),
    header_text       = header_text,
    text_raw          = body_text
  )
}


safe_group_value <- function(x, fallback = "Unknown") {
  x <- as.character(x)
  x[is.na(x) | stringr::str_squish(x) == ""] <- fallback
  x
}

# Compile list of files
text_files <- fs::dir_ls(
  TEXT_DIR,
  recurse = TRUE,
  regexp = "\\.(txt|text|md|html?)$",
  type = "file"
)

# Make corpus
documents <- purrr::map_dfr(
  text_files,
  parse_full_text_record
) |>
  mutate(
    doc_id = dplyr::coalesce(
      item_key,
      filename_stem
    ),
    doc_id = make.unique(doc_id),
    sha256 = digest::digest(
      text_raw,
      algo = "sha256"
    ),
    text_clean = clean_text(text_raw),
    analysis_word_count = stringr::str_count(
      text_clean,
      "\\S+"
    ),
    author       = safe_group_value(author),
    publication  = safe_group_value(publication),
    section      = safe_group_value(section),
    item_type    = safe_group_value(item_type),
    year_group   = safe_group_value(year),
    author_group = safe_filename(author),
    publication_group = safe_filename(publication),
    section_group     = safe_filename(section),
    text_status       = safe_group_value(text_status)
  ) |>
  select(
    doc_id,
    item_key,
    filename,
    filename_stem,
    path,
    title,
    subtitle,
    author_raw,
    author,
    published_raw,
    published_date,
    year,
    year_group,
    publication,
    publication_group,
    section,
    section_group,
    item_type,
    access,
    url,
    source_word_count,
    retrieved,
    text_status,
    text_strategy,
    text_words,
    analysis_word_count,
    text_sha256,
    sha256,
    header_text,
    text_raw,
    text_clean,
    everything()
)

# Backup Modeling Data Set to CSV
readr::write_csv(
  documents 
  # |>
  #   select(
  #     doc_id,
  #     item_key,
  #     filename,
  #     title,
  #     author,
  #     year,
  #     publication,
  #     section,
  #     item_type,
  #     text_status,
  #     analysis_word_count,
  #     sha256
  #   )
  ,
  file = paste0(DATA_DIR, "/full_text_manifest.csv"))


# Full Text Counts by Slice -----------------------------------------------
full_text_counts_by_year <- documents |>
  count(
    year_group,
    name = "full_text_records"
  ) |>
  arrange(year_group)

full_text_counts_by_author <- documents |>
  count(
    author,
    name = "full_text_records"
  ) |>
  arrange(desc(full_text_records))

full_text_counts_by_publication <- documents |>
  count(
    publication,
    name = "full_text_records"
  ) |>
  arrange(desc(full_text_records))

full_text_counts_by_section <- documents |>
  count(
    section,
    name = "full_text_records"
  ) |>
  arrange(desc(full_text_records))

readr::write_csv(
  full_text_counts_by_year,
  file = paste0(
    DATA_DIR,
    "/comparison",
    "/full_text_counts_by_year.csv"
  )
)

readr::write_csv(
  full_text_counts_by_author,
  file = paste0(
    DATA_DIR,
    "/comparison",
    "/full_text_counts_by_author.csv"
  )
)

readr::write_csv(
  full_text_counts_by_publication,
  file = paste0(
    DATA_DIR,
    "/comparison",
    "full_text_counts_by_publication.csv"
  )
)

readr::write_csv(
  full_text_counts_by_section,
  file = paste0(
    DATA_DIR,
    "/comparison",
    "/full_text_counts_by_section.csv"
  )
)


# Viz of Counts by Slice --------------------------------------------------
make_osi_table <- function(data, title = NULL, subtitle = NULL) {
  escape_html <- function(x) {
    x <- as.character(x)
    x <- gsub("&", "&amp;", x, fixed = TRUE)
    x <- gsub("<", "&lt;", x, fixed = TRUE)
    x <- gsub(">", "&gt;", x, fixed = TRUE)
    x <- gsub('"', "&quot;", x, fixed = TRUE)
    x
  }
  
  title_html <- if (!is.null(title)) {
    paste0(
      "<h2 class=\"osi-table-title\">",
      escape_html(title),
      "</h2>"
    )
  } else {
    ""
  }
  
  subtitle_html <- if (!is.null(subtitle)) {
    paste0(
      "<p class=\"osi-table-subtitle\">",
      escape_html(subtitle),
      "</p>"
    )
  } else {
    ""
  }
  
  table_html <- knitr::kable(
    data,
    format = "html",
    escape = TRUE,
    table.attr = 'class="osi-table"'
  )
  
  paste(
    title_html,
    subtitle_html,
    as.character(table_html),
    sep = "\n"
  )
}


osi_table_css <- '
<style>
* {
  box-sizing: border-box;
}

body {
  margin: 24px;
  font-family: Arial, Helvetica, sans-serif;
  color: #1D2939;
  background: white;
}

.osi-table-title {
  margin: 0 0 5px 0;
  color: #17324D;
  font-size: 22px;
  line-height: 1.2;
}

.osi-table-subtitle {
  margin: 0 0 14px 0;
  color: #667085;
  font-size: 14px;
  line-height: 1.3;
}

table.osi-table {
  width: auto;
  max-width: 100%;
  border-collapse: collapse;
  border-spacing: 0;
  font-size: 15px;
  line-height: 1.3;
}

table.osi-table th,
table.osi-table td {
  padding: 8px 12px;
  border-bottom: 1px solid #D0D5DD;
  vertical-align: middle;
}

table.osi-table thead th {
  background: #17324D;
  color: white;
  font-weight: bold;
  text-align: left;
  white-space: nowrap;
}

table.osi-table tbody tr:nth-child(even) {
  background: #F7F9FB;
}

table.osi-table tbody tr:nth-child(odd) {
  background: white;
}

table.osi-table tbody td:first-child {
  text-align: left;
  white-space: normal;
}

table.osi-table tbody td:not(:first-child) {
  text-align: right;
  white-space: nowrap;
}
</style>
'


save_osi_table <- function(table_html, filename) {
  
  table_dir <- file.path(DATA_DIR, "tables")
  
  dir.create(
    table_dir,
    recursive = TRUE,
    showWarnings = FALSE
  )
  
  output_path <- file.path(table_dir, filename)
  
  writeLines(
    c(
      "<!doctype html>",
      "<html>",
      "<head>",
      '<meta charset="utf-8">',
      osi_table_css,
      "</head>",
      "<body>",
      table_html,
      "</body>",
      "</html>"
    ),
    con = output_path,
    useBytes = TRUE
  )
  
  message("Saved: ", normalizePath(output_path, mustWork = FALSE))
  
  invisible(output_path)
}

osi_palette <- c(
  navy = "#17324D",
  dark = "#1D2939"
)

# Save HTML and PNG versions of an OSI table
save_osi_table_png <- function(table_html, html_filename) {
  html_path <- save_osi_table(table_html, html_filename)
  
  png_path <- file.path(
    dirname(html_path),
    sub("\\.html$", ".png", basename(html_path))
  )
  
  webshot2::webshot(
    url = normalizePath(html_path),
    file = png_path,
    selector = "table",
    vwidth = 1000,
    zoom = 3,
    delay = 0.5
  )
  
  invisible(png_path)
}

format_table_numbers <- function(x) {
  format(
    x,
    big.mark = ",",
    scientific = FALSE,
    trim = TRUE
  )
}


# Table 1: Corpus overview
corpus_overview <- tibble(
  Measure = c(
    "Full-text records",
    "Unique authors",
    "Unique publications",
    "Unique sections",
    "Earliest publication year",
    "Latest publication year",
    "Median article length",
    "Total analyzed words"
  ),
  Value = c(
    nrow(documents),
    dplyr::n_distinct(documents$author),
    dplyr::n_distinct(documents$publication),
    dplyr::n_distinct(documents$section),
    ifelse(
      all(is.na(documents$year)),
      "Not available",
      format(min(documents$year, na.rm = TRUE), big.mark = ",")
    ),
    ifelse(
      all(is.na(documents$year)),
      "Not available",
      format(max(documents$year, na.rm = TRUE), big.mark = ",")
    ),
    format(
      median(documents$analysis_word_count, na.rm = TRUE),
      big.mark = ",",
      scientific = FALSE
    ),
    format(
      sum(documents$analysis_word_count, na.rm = TRUE),
      big.mark = ",",
      scientific = FALSE
    )
  )
)

table_overview <- make_osi_table(
  corpus_overview,
  title = "Corpus overview",
  subtitle = "Summary of the OSI full-text article collection"
)

save_osi_table_png(
  table_overview,
  "table_01_corpus_overview.html"
)


# Table 2: Articles by year
table_year_data <- full_text_counts_by_year |>
  dplyr::transmute(
    `Publication year`  = year_group,
    `Full-text records` = full_text_records
  ) #|>
  #format_table_numbers("Full-text records")

table_year <- make_osi_table(
  table_year_data,
  title = "Articles by publication year",
  subtitle = "Full-text records available for analysis"
)

save_osi_table_png(
  table_year,
  "table_02_articles_by_year.html"
)

# Table 3: Top authors
table_authors_data <- full_text_counts_by_author |>
  dplyr::slice_head(n = 20) |>
  dplyr::mutate(Rank = dplyr::row_number()) |>
  dplyr::transmute(
    Rank,
    Author = as.character(author),
    `Full-text records` = full_text_records
  ) #|>
  #format_table_numbers("Full-text records")

table_authors <- make_osi_table(
  table_authors_data,
  title = "Most represented authors",
  subtitle = "Top 20 authors by number of full-text records"
)

save_osi_table_png(
  table_authors,
  "table_03_top_authors.html"
)


# Table 4: Publications
table_publications_data <- full_text_counts_by_publication |>
  dplyr::slice_head(n = 15) |>
  dplyr::mutate(Rank = dplyr::row_number()) |>
  dplyr::transmute(
    Rank,
    Publication = as.character(publication),
    `Full-text records` = full_text_records
  ) 

table_publications <- make_osi_table(
  table_publications_data,
  title = "Articles by publication",
  subtitle = "Top 15 publications represented in the corpus"
)

save_osi_table_png(
  table_publications,
  "table_04_articles_by_publication.html"
)


# Table 5: Sections
table_sections_data <- full_text_counts_by_section |>
  dplyr::slice_head(n = 15) |>
  dplyr::mutate(Rank = dplyr::row_number()) |>
  dplyr::transmute(
    Rank,
    Section = as.character(section),
    `Full-text records` = full_text_records
  ) 

table_sections <- make_osi_table(
  table_sections_data,
  title = "Articles by section",
  subtitle = "Top 15 sections represented in the corpus"
)

save_osi_table_png(
  table_sections,
  "table_05_articles_by_section.html"
)


# Structural Topic Modeling ------------------------------------------------
sum(is.na(documents$year))
documents <- documents[!is.na(documents$year),]

# Keep documents with enough text for topic modeling
stm_data <- documents |>
  filter(
    !is.na(text_clean),
    str_length(text_clean) >= 200
  ) |>
  mutate(
    section_model = safe_group_value(section)
  )

# Collapse infrequent sections into "Other" for interpretable effect plots
top_sections <- stm_data |>
  count(section_model, sort = TRUE) |>
  slice_head(n = 3) |>
  pull(section_model)

stm_data <- stm_data |>
  mutate(
    section_model = if_else(
      section_model %in% top_sections,
      section_model,
      "Other"
    ),
    section_model = factor(section_model)
  )

# Preprocess text for STM
processed <- stm::textProcessor(
  documents = stm_data$text_clean,
  metadata  = stm_data |>
    select(doc_id, year, section_model),
  lowercase         = TRUE,
  removestopwords   = TRUE,
  removenumbers     = TRUE,
  removepunctuation = TRUE,
  stem              = F,
  ucp               = T,
  wordLengths       = c(3, 20),
  customstopwords   = c("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
                             "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
                             "eighteen", "nineteen", "twenty", "now", "can",
                             "said", "also")
)

prepared <- stm::prepDocuments(
  processed$documents,
  processed$vocab,
  processed$meta,
  lower.thresh = 10
)

# Fit the structural topic model
set.seed(42)

STM_K <- 5

stm_model <- stm::stm(
  documents  = prepared$documents,
  vocab      = prepared$vocab,
  K          = STM_K,
  prevalence = ~ year + section_model,
  data       = prepared$meta,
  max.em.its = 12,         # 10 25 75
  init.type  = "Spectral", # Spectral Random LDA
  verbose    = FALSE
)

saveRDS(
  stm_model,
  file.path(DATA_DIR, "topics", "osi_stm_model.rds")
)

summary(stm_model) #stm_model$theta

# Topic labels and representative words
topic_labels <- stm::labelTopics(stm_model, n = N_TOP_TERMS)

custom_labels <- c(
  "War & American History",
  "OSI Activities",
  "Psychology & Lifestyle",
  "Science",
  "Moral Philosophy"
)

topic_terms <- tibble(
  topic = seq_len(STM_K),
  probability = apply(
    topic_labels$prob,
    1,
    paste,
    collapse = ", "
  ),
  frex = apply(
    topic_labels$frex,
    1,
    paste,
    collapse = ", "
  ),
  lift = apply(
    topic_labels$lift,
    1,
    paste,
    collapse = ", "
  ),
  score = apply(
    topic_labels$score,
    1,
    paste,
    collapse = ", "
  )
)

write.csv(
  topic_terms,
  paste0(DATA_DIR, "/topics/", "topic_terms.csv")
)

# Plot the first N FREX words for every topic
n_terms <- 5

frex_plot_data <- map_dfr(
  seq_len(STM_K),
  function(topic_number) {
    tibble(
      topic = custom_labels[topic_number], #paste("Topic", topic_number),
      term = topic_labels$frex[topic_number, seq_len(n_terms)],
      rank = n_terms:1
    )
  }
)


frex_plot <- ggplot(
  frex_plot_data,
  aes(
    x = reorder_within(term, rank, topic),
    y = rank,
    fill = topic
  )
) +
  geom_col(show.legend = FALSE) +
  facet_wrap(~ topic, scales = "free_y") +
  coord_flip() +
  scale_x_reordered() +
  labs(
    title = "Representative STM terms by topic",
    subtitle = "FREX terms balance frequency and exclusivity",
    x = NULL,
    y = "Relative rank"
  ) +
  theme_minimal(base_size = 11)
frex_plot

ggsave(
  file.path(FIGURE_DIR, "stm_topic_terms_frex.png"),
  frex_plot,
  width = 14,
  height = 10,
  dpi = 300
)

# Overall topic prevalence
topic_prevalence <- tibble(
  topic = seq_len(STM_K),
  mean_proportion = colMeans(stm_model$theta)
) |>
  arrange(desc(mean_proportion))

write_csv(
  topic_prevalence,
  file.path(DATA_DIR, "topics", "topic_prevalence.csv")
)

prevalence_plot <- ggplot(
  topic_prevalence,
  aes(
    x = reorder(custom_labels[topic], mean_proportion),
    y = mean_proportion
  )
) +
  geom_col(fill = osi_palette["navy"]) +
  coord_flip() +
  scale_y_continuous(labels = scales::percent) +
  labs(
    title = "Overall topic prevalence",
    x = NULL,
    y = "Mean document proportion"
  ) +
  theme_minimal(base_size = 11)
prevalence_plot #ugly

ggsave(
  file.path(FIGURE_DIR, "stm_topic_prevalence.png"),
  prevalence_plot,
  width = 9,
  height = 7,
  dpi = 300
)

# STM's built-in prevalence summary
png(
  file.path(FIGURE_DIR, "stm_summary.png"),
  width = 1800,
  height = 1400,
  res = 220
)

plot(
  stm_model,
  type = "summary",
  n = STM_K
)

dev.off()

# Topic-correlation network
topic_correlations <- stm::topicCorr(stm_model)

png(
  file.path(FIGURE_DIR, "stm_topic_correlations.png"),
  width = 1800,
  height = 1400,
  res = 220
)

plot(topic_correlations)

dev.off()

# Topic prevalence by section

section_effects <- stm::estimateEffect(
  #topics = seq_len(STM_K),
  formula = ~ section_model,
  stmobj = stm_model,
  metadata = prepared$meta,
  uncertainty = "Global"
)

png(
  file.path(FIGURE_DIR, "stm_prevalence_by_section.png"),
  width = 2200,
  height = 1600,
  res = 220
)

plot(
  section_effects,
  covariate = "section_model",
  topics = seq_len(STM_K),
  method = "pointestimate",
  xlab = "Section",
  main = "Estimated topic prevalence by section"
)

dev.off()

# Save document-topic proportions
document_topics <- as_tibble(stm_model$theta) |>
  setNames(paste0("topic_", seq_len(STM_K))) |>
  mutate(
    doc_id = prepared$meta$doc_id,
    .before = 1
  )

write_csv(
  document_topics,
  file.path(DATA_DIR, "topics", "document_topic_proportions.csv")
)

# Topic and Author Trends over Time ---------------------------------------
document_topics <- document_topics |>
  left_join(
    stm_data |>
      select(
        doc_id,
        author,
        publication,
        year
      ),
    by = "doc_id"
  ) |>
  mutate(
    author = safe_group_value(author),
    publication = safe_group_value(publication),
    year = as.integer(year)
  )

document_topics_long <- document_topics |>
  pivot_longer(
    cols = starts_with("topic_"),
    names_to = "topic",
    values_to = "topic_proportion"
  ) |>
  mutate(
    topic_number = readr::parse_number(topic),
    topic_label = paste("Topic", topic_number),
    year = as.integer(year)
  )

if (length(custom_labels) != STM_K) {
  custom_labels <- paste("Topic", seq_len(STM_K))
}

topic_lookup <- tibble(
  topic_number = seq_len(STM_K),
  topic_label = custom_labels,
  frex_terms = purrr::map_chr(
    seq_len(STM_K),
    function(i) {
      paste(
        topic_labels$frex[
          i,
          seq_len(min(5, ncol(topic_labels$frex)))
        ],
        collapse = ", "
      )
    }
  )
)

document_topics_metadata_check <- document_topics |>
  summarise(
    documents = n(),
    missing_authors = sum(
      is.na(author) | author == "Unknown"
    ),
    missing_years = sum(is.na(year)),
    missing_publications = sum(
      is.na(publication) | publication == "Unknown"
    )
  )

write_csv(
  document_topics_metadata_check,
  file.path(
    DATA_DIR,
    "topics",
    "document_topics_metadata_check.csv"
  )
)

topic_summary_table <- topic_prevalence |>
  left_join(
    topic_lookup,
    by = c("topic" = "topic_number")
  ) |>
  arrange(desc(mean_proportion)) |>
  mutate(
    Rank = row_number(),
    `Mean prevalence` = scales::percent(
      mean_proportion,
      accuracy = 0.1
    )
  ) |>
  transmute(
    Rank,
    Topic = topic_label,
    `Representative FREX terms` = frex_terms,
    `Mean prevalence`
  )

table_topics <- make_osi_table(
  topic_summary_table,
  title = "STM topic summary",
  subtitle = "Topics ranked by average document prevalence"
)

save_osi_table_png(
  table_topics,
  "table_06_topic_summary.html"
)

write_csv(
  topic_summary_table,
  file.path(
    DATA_DIR,
    "topics",
    "topic_summary_table.csv"
  )
)

document_topics_long <- document_topics_long |>
  mutate(
    topic_label = factor(
      custom_labels[topic_number],
      levels = custom_labels
    )
  )

topic_by_year <- document_topics_long |>
  filter(!is.na(year)) |>
  group_by(
    year,
    topic_number,
    topic_label
  ) |>
  summarise(
    mean_proportion = mean(
      topic_proportion,
      na.rm = TRUE
    ),
    documents = n_distinct(doc_id),
    .groups = "drop"
  ) |>
  mutate(
    topic_label = factor(
      topic_label,
      levels = custom_labels
    )
  )

write_csv(
  topic_by_year |>
    mutate(topic_label = as.character(topic_label)),
  file.path(
    DATA_DIR,
    "topics",
    "topic_prevalence_by_year.csv"
  )
)

leading_topic_by_year <- topic_by_year |>
  group_by(year) |>
  slice_max(
    order_by = mean_proportion,
    n = 1,
    with_ties = FALSE
  ) |>
  ungroup() |>
  left_join(
    topic_lookup |>
      select(topic_number, frex_terms),
    by = "topic_number"
  ) |>
  transmute(
    Year = year,
    `Leading topic` = as.character(topic_label),
    `Mean prevalence` = scales::percent(
      mean_proportion,
      accuracy = 0.1
    ),
    `Representative FREX terms` = frex_terms,
    Documents = documents
  )

table_leading_topics <- make_osi_table(
  leading_topic_by_year,
  title = "Leading topic by year",
  subtitle = paste(
    "The most prevalent STM topic in each",
    "publication year"
  )
)

save_osi_table_png(
  table_leading_topics,
  "table_07_leading_topic_by_year.html"
)

topic_trend_plot <- ggplot(
  topic_by_year,
  aes(
    x = year,
    y = mean_proportion,
    color = topic_label,
    group = topic_label
  )
) +
  geom_line(linewidth = 0.9) +
  geom_point(size = 1.8) +
  scale_y_continuous(
    labels = scales::percent_format(
      accuracy = 1
    )
  ) +
  scale_color_brewer(
    palette = "Dark2",
    drop = FALSE
  ) +
  labs(
    title = "Topic prevalence over time",
    subtitle = paste(
      "Average document-level topic proportions",
      "by publication year"
    ),
    x = NULL,
    y = "Mean topic proportion",
    color = "Topic"
  ) +
  theme_minimal(base_size = 11) +
  theme(
    legend.position = "bottom",
    panel.grid.minor = element_blank()
  )

print(topic_trend_plot)

ggsave(
  file.path(
    FIGURE_DIR,
    "topic_prevalence_over_time.png"
  ),
  topic_trend_plot,
  width = 12,
  height = 8,
  dpi = 300
)

# Topic and Author Trends over Time ---------------------------------------

if (exists("topic_heatmap", inherits = FALSE)) {
  rm(topic_heatmap)
}

topic_heatmap <- ggplot(
  topic_by_year,
  aes(
    x = year,
    y = forcats::fct_rev(topic_label),
    fill = mean_proportion
  )
) +
  geom_tile(
    color = "white",
    linewidth = 0.25
  ) +
  scale_fill_viridis_c(
    option = "C",
    labels = scales::percent_format(
      accuracy = 1
    ),
    name = "Mean\nprevalence"
  ) +
  labs(
    title = "Topic prevalence by year",
    subtitle = paste(
      "Lighter cells indicate greater average",
      "topic prevalence"
    ),
    x = NULL,
    y = "Topic"
  ) +
  theme_minimal(base_size = 11) +
  theme(
    panel.grid = element_blank()
  )

print(topic_heatmap)

ggsave(
  file.path(
    FIGURE_DIR,
    "topic_prevalence_heatmap.png"
  ),
  topic_heatmap,
  width = 12,
  height = 8,
  dpi = 300
)

author_levels <- sort(unique(top_authors))

heatmap_years <- seq(
  min(author_by_year_top$year, na.rm = TRUE),
  max(author_by_year_top$year, na.rm = TRUE)
)

author_heatmap_data <- author_by_year_top |>
  tidyr::complete(
    author = author_levels,
    year = heatmap_years,
    fill = list(articles = 0)
  ) |>
  mutate(
    author = factor(
      author,
      levels = rev(author_levels)
    ),
    articles = replace_na(articles, 0)
  )

author_heatmap <- ggplot(
  author_heatmap_data,
  aes(
    x = year,
    y = author,
    fill = articles
  )
) +
  geom_tile(
    color = "white",
    linewidth = 0.3
  ) +
  scale_fill_gradient(
    low = "white",
    high = "#D7301F",
    trans = "sqrt",
    breaks = scales::pretty_breaks(n = 5),
    name = "Articles"
  ) +
  scale_x_continuous(
    breaks = scales::pretty_breaks(n = 10),
    expand = expansion(add = 0)
  ) +
  labs(
    title = "Author presence over time",
    subtitle = paste(
      "Number of articles by year for leading authors;",
      "authors shown in alphabetical order"
    ),
    x = NULL,
    y = NULL
  ) +
  theme_minimal(base_size = 11) +
  theme(
    panel.grid = element_blank(),
    axis.text.y = element_text(size = 9),
    legend.position = "right"
  )

print(author_heatmap)

ggsave(
  file.path(
    FIGURE_DIR,
    "author_presence_over_time.png"
  ),
  author_heatmap,
  width = 12,
  height = 8,
  dpi = 300
)

author_topic <- document_topics_long |>
  filter(
    !is.na(author),
    author != "",
    author != "Unknown",
    author %in% top_authors
  ) |>
  group_by(
    author,
    topic_number,
    topic_label
  ) |>
  summarise(
    mean_proportion = mean(
      topic_proportion,
      na.rm = TRUE
    ),
    documents = n_distinct(doc_id),
    .groups = "drop"
  ) |>
  mutate(
    topic_label = factor(
      topic_label,
      levels = custom_labels
    )
  )

write_csv(
  author_topic |>
    mutate(topic_label = as.character(topic_label)),
  file.path(
    DATA_DIR,
    "topics",
    "author_topic_associations.csv"
  )
)

author_levels <- sort(unique(as.character(author_topic$author)))

author_topic <- author_topic |>
  mutate(
    author = factor(
      author,
      levels = rev(author_levels)
    )
  )

author_topic_plot <- ggplot(
  author_topic,
  aes(
    x = author,
    y = mean_proportion,
    fill = topic_label
  )
) +
  geom_col(
    position = "stack"
  ) +
  coord_flip() +
  scale_y_continuous(
    labels = scales::percent_format(
      accuracy = 1
    )
  ) +
  scale_fill_brewer(
    palette = "Dark2",
    drop = FALSE
  ) +
  labs(
    title = "Topic composition of leading authors",
    subtitle = paste(
      "Average STM topic proportions across each author's documents;",
      "authors shown in alphabetical order"
    ),
    x = NULL,
    y = "Mean topic proportion",
    fill = "Topic"
  ) +
  theme_minimal(base_size = 11) +
  theme(
    legend.position = "bottom",
    panel.grid.minor = element_blank()
  )

print(author_topic_plot)

ggsave(
  file.path(
    FIGURE_DIR,
    "author_topic_composition.png"
  ),
  author_topic_plot,
  width = 12,
  height = 8,
  dpi = 300
)
