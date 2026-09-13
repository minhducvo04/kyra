# YouTube AI Learning Reels — Project Brief and Conversation Handoff

**Prepared for:** Minh Duc Vo  
**Date:** September 13, 2026  
**Purpose:** Preserve the complete substance of the product discussion so development can continue in Codex without losing context.

> This document summarizes technical and product decisions and provides general legal-risk analysis. It is not legal advice.

## 1. Original Product Idea

Build software that discovers high-view YouTube lectures containing excellent explanations, examples, or visualizations and converts the most educational moments into short, engaging learning reels.

The envisioned experience has two forms:

1. **Private use:** Minh and a small invited group can view selected moments from the original lecture.
2. **Potential public use:** The system creates an independently generated educational reel that teaches the same underlying concept using new narration, visuals, wording, numbers, and presentation—not a repost of the original footage.

The long-term goal is not merely an educational TikTok clone. It is a short-form learning system that adds questions, feedback, rewards, spaced review, and defensible measurements of learning.

Minh wants to plan for success from the beginning: even if the first version is for a small private group, its architecture should not make a lawful public version impossible later.

## 2. Current Requirements and Preferences

- Automatically discover promising, popular lectures.
- Identify especially useful visualizations, examples, demonstrations, analogies, equations, and surprising results.
- Summarize and analyze lecture content.
- Select useful start and end timestamps.
- For private use, preserve access to the original explanation.
- For public use, generate a new video teaching the concept rather than reposting the original.
- Make questions optional and allow users to turn them on or off.
- Provide immediate, educational feedback for answers.
- Add rewards, progress tracking, and weekly comparisons.
- Prevent the application from automatically uploading material to YouTube.
- Discourage users from extracting or redistributing private generated clips.
- Keep licensing and copyright concerns in the architecture from the start.
- Automate batch processing so videos do not need to be downloaded and processed manually one at a time.

## 3. Overall Viability Assessment

| Version | Technical viability | Legal/platform viability | Assessment |
| --- | ---: | ---: | --- |
| Download arbitrary lectures and repost short clips | 9/10 | 2/10 | Easy technically, poor foundation |
| Independently recreate concepts with original narration and visuals | 8/10 | 7/10 | Strong core direction |
| Work with creator-authorized, CC-licensed, or public-domain sources | 9/10 | 9/10 | Best public/scalable direction |
| Private study experience using timestamped YouTube embeds | 9/10 | 8/10 | Safest arbitrary-YouTube path |
| Private pipeline downloading arbitrary YouTube videos | 9/10 | Uncertain; platform-noncompliant | Practically possible but not cleanly scalable |

For a small-group product, the engineering is very achievable. The difficult parts are source access, rights management, reliable pedagogical selection, question quality, and honest learning measurement—not storage itself.

## 4. The Critical Legal and Platform Distinctions

Three different issues must not be merged:

### 4.1 Technical accessibility

Public tools such as `yt-dlp`, `youtube-dl`, and `pytube` can often download publicly viewable YouTube videos. Their availability proves technical feasibility, not authorization.

### 4.2 YouTube's contract and platform rules

YouTube's general Terms restrict downloading, reproducing, altering, or using content except when the service itself permits it, the relevant rights holders grant permission, or applicable law permits it.

YouTube's official API policies separately state that API clients must not download, import, cache, or store YouTube audiovisual content without prior approval. They also restrict scraping and use of undocumented access mechanisms.

Therefore, avoiding the official API does not make the general YouTube Terms disappear.

### 4.3 Copyright law

Copyright generally protects a lecturer's expression, including:

- Recorded footage and audio
- Exact narration and distinctive phrasing
- Original illustrations and animations
- Particular diagrams
- Potentially the distinctive selection, order, and structure of an example

Copyright generally does not protect the underlying:

- Facts
- Ideas
- Equations
- Systems
- Methods
- General concepts

A public reel is safer when it extracts the idea and independently teaches it. Merely changing the style while preserving the original narration, numbers, diagram sequence, and structure may remain too close.

### 4.4 Fair-use position of the proposed analysis

The proposed use has favorable characteristics:

- Educational purpose
- Initially private and noncommercial
- Source used for research or analysis
- No original footage or audio in public output
- New teaching expression and added questions
- Potential to direct learners to the full original lecture

The U.S. Copyright Office's 2025 report on generative-AI training states that noncommercial research or analysis that does not enable portions of the works to be reproduced in outputs is likely to be fair use. It also emphasizes that fair use depends on the particular facts and that some AI uses qualify while others do not.

This supports the project's general transformative-analysis argument, but it is not a court ruling about this exact application. A copyright argument also does not automatically resolve YouTube's contractual restrictions.

### 4.5 What “public” means

A publicly viewable video is not necessarily public-domain material. Public access permits viewing under the platform's conditions; it does not inherently grant copying, redistribution, or derivative-work rights.

## 5. OpenAI and Reddit Comparison

Minh observed that AI companies have trained on enormous amounts of publicly accessible internet material, including online conversations, which can appear more invasive than analyzing public lectures.

That criticism is understandable. The legal boundary around AI training remains contested, and large companies can accept litigation and licensing costs that small developers cannot.

However, the current OpenAI–Reddit relationship includes a formal partnership announced in 2024 under which OpenAI accesses Reddit's structured Data API. This is not equivalent to a general rule that anything publicly visible can automatically be copied without restriction. It also does not resolve every question about earlier datasets or public-web training.

The practical conclusion is not that Minh's idea is inherently improper. It is that large-company behavior is not reliable legal permission for a smaller application's ingestion method.

## 6. Official YouTube API vs. Third-Party Downloaders

### 6.1 Official YouTube APIs

These normally use a Google Cloud project, API key, or OAuth:

- **YouTube Data API:** Search, metadata, channels, playlists, views, duration, license field, and embeddable status.
- **IFrame Player API:** Official embedded playback and start/end control.
- **Captions API:** Caption management and downloads for users authorized to edit the relevant video.
- **Analytics API:** Statistics for authorized channels.

The official Data API does not offer a general endpoint for downloading arbitrary public videos. The official caption-download endpoint requires the user to have permission to edit the video.

### 6.2 Third-party Python tools

Tools such as `yt-dlp` typically inspect YouTube's web/player delivery mechanisms, obtain underlying media stream URLs, download separate streams, and often invoke FFmpeg to combine audio and video. Some tools use undocumented internal player interfaces.

They do not become official merely because they are distributed as public Python modules. Their operation may avoid the official Data API, but the general YouTube Terms still apply.

### 6.3 Where the official API would appear in this project

Use it for compliant discovery and organization:

```text
Search educational topics
→ retrieve candidate video IDs
→ collect views, duration, channel, date, license, and embeddable status
→ rank candidates
→ retain the source URL and metadata
```

Do not confuse this metadata workflow with obtaining the audiovisual file.

## 7. Recommended Rights-Aware Architecture

Every source should be assigned a rights state before ingestion:

```text
EMBED_ONLY
OWNER_AUTHORIZED
CC_BY_DIRECT_SOURCE
PUBLIC_DOMAIN
RIGHTS_UNCLEAR
REJECTED
```

Rules:

- `OWNER_AUTHORIZED`, `CC_BY_DIRECT_SOURCE`, and `PUBLIC_DOMAIN` may enter the download/cut pipeline, subject to their exact license conditions.
- `EMBED_ONLY` retains only the YouTube ID, timestamps, metadata, internal concept notes, questions, and independently generated material.
- `RIGHTS_UNCLEAR` cannot be publicly released until resolved.
- `REJECTED` cannot be processed or published.
- Public release additionally requires an explicit `PUBLIC_APPROVED` review state.

Even when a YouTube video is marked Creative Commons, copyright permission and platform download permission should be treated separately. Ideally, obtain the file from the creator or another authorized direct source.

## 8. Proposed Content Pipeline

```text
Discovery
→ Metadata collection
→ Quality ranking
→ Rights gate
→ Authorized ingestion or embed-only registration
→ Transcription or supplied transcript
→ Topic segmentation
→ Visual/OCR analysis
→ Educational-moment ranking
→ Candidate timestamps
→ Concept extraction
→ Question generation
→ Independent visualization generation
→ Similarity and accuracy checks
→ Human review
→ Private release
→ Optional public-rights approval
```

### 8.1 Lecture discovery

Potential quality signals:

- View count adjusted for video age
- Likes and meaningful engagement where available
- Educational keywords
- Channel credibility
- University or instructor affiliation
- Description and chapter quality
- Recency where the topic can become outdated
- Low clickbait probability
- Comments indicating memorable timestamps

High views should not be treated as equivalent to high educational quality.

Example ranking idea:

```text
quality_score =
    view_score
  + engagement_score
  + educational_keyword_score
  + channel_credibility
  + transcript_or_description_quality
  - clickbait_penalty
  - outdated_information_penalty
```

### 8.2 Efficient authorized-video processing

Do not send every full-resolution frame to an AI model. For an authorized lecture:

1. Ingest the source once.
2. Extract 16 kHz mono audio for speech recognition.
3. Create a temporary low-resolution analysis proxy.
4. Detect slide and scene changes.
5. Sample frames around visual changes.
6. Run OCR on equations, labels, and slides.
7. Segment the transcript by topic.
8. Perform expensive multimodal analysis only on promising intervals.
9. Cut high-quality segments after selection.
10. Retain or delete source artifacts according to authorization and retention policy.

### 8.3 Batch-processing infrastructure

```text
Discovery worker
→ Rights-check worker
→ Ingestion worker
→ Transcription worker
→ Visual-analysis worker
→ Highlight-ranking worker
→ Question-generation worker
→ Human-review queue
```

Engineering requirements:

- Resumable jobs
- Two to four concurrent workers for the first version
- Content hashes and deduplication
- Idempotent stages
- Per-stage failure status
- Retry limits
- Temporary-file cleanup
- Encrypted object storage
- Separation between original and generated assets

Storage is manageable. Bandwidth, transcription cost, video-generation cost, and permission tracking are more likely to become bottlenecks.

## 9. Selecting Educational Moments

Useful signals include:

- “For example…” or “Here is the intuition…” language
- A surprising result
- A common misconception followed by correction
- A visible before-and-after transformation
- A diagram or equation developing over time
- A demonstration or experiment
- A concrete analogy
- A question followed by explanation
- A result that is understandable with few prerequisites
- Changes in voice emphasis or speaking pace
- Comment activity concentrated around a timestamp

Each candidate can be scored separately:

```text
educational_value
visual_value
self_containment
question_potential
accuracy_confidence
copyright_similarity_risk
```

The model should propose:

- Start and end timestamps
- Learning objective
- Key idea
- Required prior context
- Factual claims and supporting source span
- Suggested question
- Answer and explanation
- Visualization plan
- Similarity-risk notes

Human review is important early because automatic systems often select an attractive moment while omitting a critical definition, assumption, or qualification.

## 10. The Concept Firewall for Public Generation

To reduce copying, public generation should not operate directly from the original frames or transcript during the rendering stage. First convert the lesson into an abstract concept representation:

```json
{
  "concept": "gradient descent overshooting",
  "learning_goal": "show why an excessively large learning rate diverges",
  "required_facts": [
    "the update moves opposite the gradient",
    "step size scales the update"
  ],
  "example_constraints": [
    "use a convex function",
    "compare two learning rates",
    "show oscillation around the minimum"
  ],
  "source_timestamp": "31:20-34:10"
}
```

Then generate from this abstraction with:

- New narration
- Different example values
- Different sequence and framing
- New visual design
- Different labels where technically appropriate
- Original or properly licensed voice
- Attribution and link to the full source lecture

Public-output checks should compare the result against the source for excessive transcript similarity, copied visual composition, matching frame sequences, and reuse of distinctive examples.

## 11. Private Experience and Embedded-Player Tradeoffs

The safest arbitrary-YouTube private experience is to store a video ID and timestamps and play the original through YouTube's official embedded player.

YouTube's IFrame API supports `startSeconds` and `endSeconds`, although playback may begin near the closest keyframe rather than the exact frame.

### Embedded-player disadvantages

| Downside | Effect | Possible mitigation |
| --- | --- | --- |
| Limited visual control | Cannot freely crop, reframe, remove branding, replace audio, or modify the player | Put the learning interface before, after, or beside the player |
| Less seamless reel UX | Embeds can load more slowly than native clips | Reuse one persistent player and prepare only the next item |
| Ads | Non-Premium users may see interruptions | Accept for source moments; keep generated lessons native |
| Availability changes | Creator can delete, region-lock, age-restrict, or disable embedding | Preserve concepts and generated assets; flag unavailable sources |
| Imperfect clip boundaries | Keyframe-based starts may not be exact | Include a small lead-in buffer |
| No raw content for AI | Embed provides playback, not arbitrary transcript/video access | Use authorized material, supplied transcript, or manual timestamps |
| External navigation | Users may open the original on YouTube | Accept; do not obscure required attribution or player behavior |
| Privacy/data exchange | Playback communicates with YouTube | Use Privacy Enhanced Mode where applicable |
| Network dependency | No independent offline playback | Cache only the app's notes and generated content |
| Platform dependency | YouTube may change API/player behavior | Keep internal concept and learning models source-independent |

Privacy Enhanced Mode uses the `youtube-nocookie.com` domain and limits how embedded viewing affects personalization, though YouTube's applicable terms still govern use.

## 12. Questions, Feedback, and Learning Modes

This is likely the product's strongest differentiation. Offer three modes:

- **Watch:** No interruption.
- **Quick Check:** One optional question after each short.
- **Learn:** Prediction, transfer, feedback, and delayed review.

A strong Learn-mode flow:

1. Show the beginning of the short.
2. Pause before the important result.
3. Ask the learner to predict what happens.
4. Continue the explanation or visualization.
5. Explain why the response was correct or incorrect.
6. Present a similar example with different values.
7. Schedule a delayed recall question one or more days later.

Question types:

- Predict what happens next
- Explain the idea in one sentence
- Select the correct visualization
- Apply the idea with different values
- Identify why a plausible answer is wrong
- Recall the idea after a delay

### Feedback design

Avoid merely showing a green check or red X.

Correct-answer feedback should briefly explain the mechanism. Incorrect-answer feedback should first provide a targeted hint based on the likely misconception and allow a retry before revealing the answer.

Example:

> Correct. Increasing the learning rate enlarges every update. Here, each update crosses the minimum by a larger amount, so the process diverges.

Example hint:

> Look at the distance traveled during each update. Is it shrinking or growing?

Question quality must be grounded in an approved concept record and evidence span. Generated questions should be checked for ambiguity, unsupported answers, multiple valid choices, and dependence on facts absent from the lesson.

## 13. Rewards and Honest Progress Measurement

The system should reward demonstrated learning more heavily than passive consumption.

Possible rewards:

| Action | Example XP |
| --- | ---: |
| Watch a short | 1 |
| Attempt a question | 3 |
| Correct initial response | 5 |
| Correct delayed recall | 10 |
| Master a concept | 20 |
| Correct a previous misconception | Bonus |

Avoid allowing immediate repeated attempts to farm points.

### Mastery definition for the MVP

A concept becomes **Mastered** only after:

- A correct initial or corrected response
- A correct response to a different form of the question
- A correct delayed-recall response after at least 24 hours
- At least one successful attempt without an answer-revealing hint

Initial states:

```text
NEW
PRACTICING
MASTERED
```

### Do not claim vague learning improvements

Avoid:

> You learned 20% more than last week.

Instead use an operationally defined statement:

> You mastered 12 concepts this week, compared with 10 last week—20% more.

Or:

> Your delayed-recall accuracy increased from 60% to 72%: 12 percentage points, or 20% relative improvement.

Useful dashboard metrics:

- Concepts mastered
- Delayed-recall accuracy
- Transfer-question accuracy
- Previously corrected misconceptions
- Active learning days
- Due reviews completed
- Confidence-calibration accuracy

Do not equate watch time, videos completed, or immediate recognition with learning.

### Minimal learner-concept data

```text
user_id
concept_id
times_seen
questions_attempted
initial_accuracy
delayed_accuracy
transfer_accuracy
hints_used
response_latency
last_reviewed_at
next_review_at
mastery_status
```

A sophisticated knowledge-tracing or item-response model is unnecessary for the first version. Simple explicit rules are easier to verify and explain.

## 14. Preventing Uploads and Redistribution

### 14.1 Preventing the application from auto-uploading

This can be enforced reliably:

- Do not request YouTube upload OAuth scopes.
- Do not implement publishing or social-export endpoints.
- Keep service credentials server-side.
- Restrict outbound services to an allowlist.
- Require explicit administrative approval before any future export capability.
- Log media access, generation, and export attempts.
- Never expose permanent public media URLs.

The app should not possess the credentials or code path necessary to upload to YouTube.

### 14.2 Preventing a viewer from copying

This cannot be guaranteed because anything viewable can be screen-recorded. It can be discouraged using:

- Streaming rather than direct file downloads
- Short-lived signed URLs
- Authentication for every media request
- HLS/DASH segmented delivery
- User-specific visible or forensic watermarks
- No raw-file endpoint
- Rate limiting and access-pattern monitoring
- Invitation-only groups
- Terms prohibiting redistribution
- FairPlay, Widevine, or PlayReady DRM only if later justified

For a small trusted group, authenticated streaming, signed URLs, and user-specific watermarking are likely enough. Full DRM adds cost and complexity without making copying impossible.

## 15. Recommended MVP

### Phase 1: Prove the learning experience

- User pastes a YouTube URL.
- App retrieves permitted metadata.
- Official player appears.
- User marks an interesting start and end time.
- User supplies notes or an authorized transcript/file.
- AI extracts a structured concept card.
- AI proposes one question and explanation.
- App generates a simple independent visualization.
- User compares the original embedded moment with the generated lesson.
- Human approves accuracy.
- Small invited group uses Watch, Quick Check, or Learn mode.
- System schedules one delayed-review question.
- Dashboard reports mastery and delayed recall.

### Phase 2: Automate authorized ingestion

- Creator upload portal
- Creator/channel authorization
- Worker queues
- Automatic transcription and OCR
- Moment ranking
- Batch processing
- Rights ledger
- Similarity checks
- Secure streaming

### Phase 3: Public-ready operation

- Explicit public-release workflow
- Creator attribution and source linking
- Takedown/objection process
- License-specific enforcement
- Accuracy and similarity audit trail
- Public generated reels only
- Original footage only where the license expressly permits it

## 16. Strongest Business Direction

A creator opt-in model could turn the principal risk into a distribution advantage:

- Professors and educational channels authorize source files.
- The system produces candidate reels and questions.
- Creators approve before publication.
- Each reel links viewers to the full lecture.
- Revenue or traffic can be shared with creators.

The defensible value is not basic clipping. It is:

- Identifying genuinely educational moments
- Converting passive video into retrieval practice
- Producing accurate independent visual explanations
- Measuring delayed retention honestly
- Maintaining provenance and permissions
- Driving learners toward deeper original material

## 17. Decisions Reached So Far

1. The core product idea is viable and worth prototyping.
2. Public output should be an independent educational reconstruction, not a lightly restyled clip.
3. Original arbitrary YouTube content should ideally remain embed-only.
4. Automatic downloading is technically easy but cannot honestly be called compliant for arbitrary YouTube sources.
5. Official APIs are useful for discovery, metadata, and embedding—not arbitrary media downloads.
6. A third-party downloader is not the same as the official YouTube API and does not eliminate the general Terms issue.
7. Every source needs a rights state and every public item needs explicit approval.
8. The question/feedback/review loop is central, not merely an accessory.
9. Progress claims must be based on demonstrated mastery or delayed recall, not watch time.
10. The application itself should contain no automatic YouTube-upload capability.
11. Viewer copying can be deterred but never completely prevented.
12. The source-ingestion adapter should be replaceable so an experimental workflow can later migrate to authorized sources without rewriting the learning system.

## 18. Important Open Questions

- Which subjects should the first prototype target?
- Is the first interface mobile, web, or both?
- Should the first visualization engine use programmatic animation, generative video, slides, or a mixture?
- Will the MVP accept only manually selected videos, or automatically discover candidates?
- Which creator-authorized or openly licensed lecture collection should seed the initial dataset?
- Who reviews technical accuracy?
- What private-group size and trust level are expected?
- What is the acceptable processing cost per source hour and per generated reel?
- How long should authorized source files be retained?
- Is the eventual business customer the learner, educator, school, or content creator?

## 19. Suggested Initial Technical Stack

This was not finalized, but a practical initial stack would be:

- **Frontend:** Next.js/TypeScript web application with mobile-responsive reel interface
- **Backend:** FastAPI/Python for media and AI orchestration, or a TypeScript API if one-language simplicity matters more
- **Database:** PostgreSQL
- **Queue:** Redis with Celery/RQ for Python, or BullMQ for TypeScript
- **Media processing:** FFmpeg
- **Speech recognition:** Whisper-compatible transcription service or local model
- **Visual analysis:** Scene-change detection, sampled frames, OCR, then multimodal-model review
- **Generated visuals:** Programmatic HTML/canvas/Remotion/Manim first; generative video only when it materially adds value
- **Storage:** Private object storage with encryption and short-lived signed delivery URLs
- **Authentication:** Invitation-only accounts initially
- **Analytics:** First-party event table rather than invasive third-party tracking

Programmatic visuals are preferable for mathematical and scientific accuracy. Generative video is better for illustrative scenes but less reliable for exact diagrams, equations, and labels.

## 20. References Discussed

- [YouTube Terms of Service](https://www.youtube.com/static?gl=US&template=terms)
- [YouTube API Services Developer Policies](https://developers.google.com/youtube/terms/developer-policies)
- [YouTube Data API video resource](https://developers.google.com/youtube/v3/docs/videos)
- [YouTube Data API search method](https://developers.google.com/youtube/v3/docs/search/list)
- [YouTube Captions download method](https://developers.google.com/youtube/v3/docs/captions/download)
- [YouTube IFrame Player API](https://developers.google.com/youtube/iframe_api_reference)
- [YouTube embed and Privacy Enhanced Mode guidance](https://support.google.com/youtube/answer/171780)
- [YouTube license types and Creative Commons guidance](https://support.google.com/youtube/answer/2797468)
- [YouTube fair-use guidance](https://support.google.com/youtube/answer/9783148)
- [U.S. Copyright Office: What Does Copyright Protect?](https://www.copyright.gov/help/faq/faq-protect.html)
- [U.S. Copyright Office: Copyright and Artificial Intelligence, Part 3](https://www.copyright.gov/ai/Copyright-and-Artificial-Intelligence-Part-3-Generative-AI-Training-Report-Pre-Publication-Version.pdf)
- [OpenAI and Reddit Partnership](https://openai.com/index/openai-and-reddit-partnership/)
- [UC San Diego Psychology: Retrieval Practice](https://psychology.ucsd.edu/undergraduate-program/undergraduate-resources/academic-writing-resources/effective-studying/retrieval-practice.html)

## 21. Ready-to-Paste Continuation Prompt for Codex

```text
I want to continue building the YouTube AI Learning Reels project described in the attached project brief. Treat the brief as the current source of truth.

The product discovers high-quality lecture moments, represents them as structured concepts, creates independent visual learning reels, asks optional retrieval and transfer questions, gives useful feedback, schedules delayed reviews, and measures demonstrated mastery. The initial version is for a small invited group, but the architecture must support a legally safer public version.

Important constraints:
- Separate official YouTube discovery/embedding from media ingestion.
- Every source must have a rights state.
- Arbitrary YouTube sources should default to EMBED_ONLY.
- Public outputs must not include original footage/audio or excessively similar expression unless specifically licensed.
- No automatic YouTube upload capability.
- Do not claim vague learning improvements; use mastery and delayed recall.
- Keep source adapters replaceable.

First, inspect the workspace and propose the smallest end-to-end MVP architecture and milestone plan. Then help me implement it incrementally, beginning with the data model and one complete vertical slice: add a source URL, define a timestamped concept, generate one question, record an answer, and update mastery status.
```

