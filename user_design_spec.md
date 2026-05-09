# user_design_spec.md

> A subjective statement of what I want from this AI-driven LED tool as a human being who plays live shows. Not architecture, not tech stack — just goals, feelings, and the shape of the experience I'm trying to build for myself. Use this as the north star when making design trade-offs.

---

## Who I am in this loop

I'm the VJ. I'm standing at a gig — possibly a festival, possibly a small room — with the rig powered up, the music playing, and a crowd in front of me. The LEDs are an instrument I'm playing in real time. I am not a programmer in this moment. I am a performer.

The tool exists to extend what I can express through light, in sync with the music, in the moment.

---

## What I actually want from this tool

### 1. Make my imagination the bottleneck, not the vocabulary

When I picture something — *"two red comets chasing each other along the top, leaving sparks that fade behind them, breathing on the kick"* — I want to type that, speak it, or describe it in a few words, and see it on the rig within seconds. I do not want to think about whether what I'm imagining "fits" the toolbox. The toolbox should never be the limit. If I can describe it clearly, the system should be able to render it.

### 2. Tune by feel, not by re-prompting

Once an idea exists on the LEDs, I want to shape it with my hands — sliders, colour pickers, a knob, a tap. *"Slightly faster. A bit more orange. Pull the bottom row down."* These are physical, embodied adjustments. I do not want to go back to a chat box and ask the AI to nudge a number; that's too slow, too disembodied, and breaks the flow of performing. The AI gives me the instrument; I play it.

### 3. Never break the show while I'm exploring

I need to be able to experiment, fail, retry, throw away — *while the dance floor keeps dancing*. The crowd should never see my drafts. There has to be a clean separation between the place where I'm creating and the place where I'm performing, and switching between them must be deliberate and instant.

### 4. Lock in to the music

The light has to *listen*. Bass, snare, hats, beats, tempo — whatever the music is doing, the rig should feel coupled to it without me babysitting every parameter. When I write a new effect with the AI, audio reactivity is not a feature I tack on; it's part of how I describe what I want from the start. *"Brightness pulsing on the kick"* should be a sentence, not a configuration screen. That said, audioreactivity can also become too much sometimes, so the master slider for audioreactivity will always be the final boss and should never be bypassed.

### 5. Trust it to stay up

This thing is going to run at venues, often on a phone hotspot, often after I've been on my feet for hours. It needs to come back up cleanly when the power blinks. It needs to stay stable for hours of continuous use. It needs to not require SSH-debugging at 2am. If something fails, the LEDs should degrade gracefully, not go black, not lock up. Reliability is part of the artistic experience — a glitchy tool kills the moment as surely as a bad idea does.

### 6. Stay legible to me at a glance

When I look at the operator UI mid-set, I need to know: what's playing right now, what's queued, how loud the room feels to the system, whether everything is connected. No deep menus, no jargon — the information I need to perform should be on one screen, big enough to read in low light, responsive to a phone or tablet.

### 7. Collaborate with me — don't replace my taste

The AI is a co-author, not a curator. I bring the aesthetic; it brings the speed. I want it to write what I asked for, not what it thinks I should want. When it gets something wrong, it should be easy and fast to correct ("no, the trail should be longer") without unwinding the whole conversation. When it gets something right, I should be able to keep that and build on it.

### 8. Persist what's good

Looks I love are looks I want to come back to — at this gig, at the next one, six months from now. I want to be able to save the things that worked, name them, recall them, and trust they'll come back exactly as I left them, even after a server restart or a fresh install at a different venue.

### 9. Mobile / hands-free where it matters

The rig is often physically across the room from me. I want to drive it from my phone, from a tablet, sometimes from the booth, sometimes while walking around. The interface should not assume I'm sitting at a laptop with two free hands.

### 10. Surprise me, sometimes

The best moments in a live set are the ones I didn't plan. If the AI can occasionally suggest a direction, riff on what's already playing, or push an idea further than I asked — without overstepping — that's a win. The tool should be capable of being a creative provocateur, not just a transcriber of my requests.

### 11. I'm a human

I sometimes need to pee, get a drink, talk to people. So I wont be on the keys the whole time. The system needs to have an auto-play mode where I can just chain good effects from a queue, decide how many loops each one goes for etc.

---

## The feel I'm chasing

A live, responsive, intimate instrument. Less "control panel," more "synth." Something that disappears into the performance — where I stop thinking about the tool and start thinking about the room.

If the system ever forces me to leave performance-mind and enter engineer-mind during a show, it has failed. Every design decision should be checked against that line.

---

## What I'm NOT trying to build

- A general-purpose lighting console for non-AI workflows. Other people do that better; I want the AI-native path.
- A tool optimised for setup-time programming. I will be writing looks *during* gigs, not weeks before them.
- A pixel-mapping suite or a video-mapping engine. The light is abstract and reactive, not literal.
- A system that requires another human in the loop (a tech, an engineer) to operate. I run this alone.

---

## How to use this document

When making technical trade-offs, ask: *does this make the human-side experience above easier or harder?* If a clean abstraction makes the tool slightly slower to respond, slower to iterate, or less expressive — pick a different abstraction. The performer's experience is the thing being optimised.
