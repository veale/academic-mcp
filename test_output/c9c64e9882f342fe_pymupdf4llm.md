# **Making Sense of Private Advertising: A Principled** **Approach to a Complex Ecosystem**


## Gabriel Kaptchuk [∗]

University of Maryland

kaptchuk@umd.edu


## Kyle Hogan

Massachusetts Institute of Technology

klhogan@csail.mit.edu


## Alishah Chator [∗]

Baruch College

alishah.chator@baruch.cuny.edu


## Mayank Varia

Boston University

varia@bu.edu

## **Abstract**


In this work, we model the end-to-end pipeline of the advertising

ecosystem, allowing us to identify two main issues with the current

trajectory of private advertising proposals. First, prior work has

largely considered ad targeting and engagement metrics individu
ally rather than in composition. This has resulted in privacy notions

that, while reasonable for each protocol in isolation, fail to compose

to a natural notion of privacy for the ecosystem as a whole, permit
ting advertisers to extract new information about the audience of

their advertisements. The second issue serves to explain the first:

we prove that _perfect_ privacy is impossible for any, even minimally,

useful advertising ecosystem, due to the advertisers’ expectation of

conducting market research on the results.

Having demonstrated that leakage is inherent in advertising, we

re-examine what privacy could realistically mean in advertising,

building on the well-established notion of _sensitive_ data in a specific

context. We identify that fundamentally new approaches are needed

when designing privacy-preserving advertising subsystems in order

to ensure that the privacy properties of the end-to-end advertising

system are well aligned with people’s privacy desires.

## **Keywords**


advertising, privacy norms, universal composability, information

leakage, attribute privacy

## **1 Introduction**


Behavioral advertising, in which people are preferentially shown

advertisements that align with their interests and demographics,

has become the financial backbone of the internet and the default

business model for large swaths of the technology sector. This

advertising ecosystem—and the user-tracking infrastructure that

powers it—are widely known to be privacy invasive [50], not only

due to the collection of sensitive, personal data, but also because of

the ways in which that data is _used_ [17, 36, 95, 96].

A great deal of research has focused on the harms caused to peo
ple through the use of their data for the targeting of advertisements.


∗Work done while at Boston University


This work is licensed under the Creative Commons Attribu
tion 4.0 International License. To view a copy of this license

[visit https://creativecommons.org/licenses/by/4.0/ or send a](https://creativecommons.org/licenses/by/4.0/)

letter to Creative Commons, PO Box 1866, Mountain View, CA 94042, USA.

_Proceedings on Privacy Enhancing Technologies 2026(1), 450–469_

© 2026 Copyright held by the owner/author(s).

[https://doi.org/10.56553/popets-2026-0023](https://doi.org/10.56553/popets-2026-0023)


## Srinivas Devadas

Massachusetts Institute of Technology

devadas@mit.edu


Targeting algorithms are designed to maximize profit for the con
stituents of the advertising industry, not to serve the best interests

of the viewer, and prior studies have shown that behavioral target
ing is used to perpetuate harmful biases [3, 35, 54, 75, 88], facilitate

the spread of disinformation [15, 34, 53, 102], and exploit sensi
tive information such as mental health data to target vulnerable

populations [6, 17, 24, 98].

While targeting has been the most widely-researched compo
nent of behavioral advertising, matching ads to users is not the

only functionality of the ecosystem. Advertisers expect to conduct

market research using the relative success of their different adver
tisements. To do so, they demand _metrics_ on how users responded

to ads—did they buy something after viewing the ad? Subscribe to

the advertiser’s mailing list? If so, which ad drove this engagement?

Ad networks keep records of this information and submit it back to

advertisers, allowing them to improve their understanding about

the preferences of their consumers and refine future ad campaigns.

Targeted advertising is not unique to digital advertising: print

advertisements have long been targeting their ads towards specific

groups of people using both the context in which the ad will be

displayed (e.g., the magazine or billboard on which to advertise)

as well as cues within the ad material itself (e.g., visuals, audio,

etc.). A famous example is the 90’s Subaru campaign that was

targeted towards the LGBT community by including, e.g., queer
coded license plates on the depicted cars [39, 65]. The shift to digital

advertising is, therefore, not a change in _type_, but a change in
_magnitude_ and _precision_ . Digital advertising, with its specific target

audiences and accurate attribution of user behavior to associated ad

views, allows advertisers to conduct market research that is far more

invasive than was possible with print media, calling into question

the ethics of campaigns that focus on sensitive audiences. This is

the case not only due to the well-understood harms of collecting

sensitive data for targeting ads [85], but also because it is currently

unclear what or how much information advertisers are able to learn

about these sensitive audiences from the metrics released on their

ad campaigns.

In response, researchers and technology companies have pro
posed a shift towards “privacy-preserving advertising systems,” a

collection of proposals [8, 13, 42, 46, 48, 51, 52, 56, 73, 74, 83, 84,

90, 92, 100, 106, 107] that aim to maintain the existing advertising

business model while making the process of (1) targeting adver
tisements and (2) reporting metrics on advertisement efficacy in a

_privacy-preserving_ manner. Proponents of privacy-preserving ad
vertising systems have made significant progress in refining the



450


Making Sense of Private Advertising Proceedings on Privacy Enhancing Technologies 2026(1)



design of these systems, some of which have even been deployed

in popular browsers [42, 69, 100].

**Unpacking “privacy” within advertising.** In this work, we take

a step back to re-examine “privacy-preserving advertising systems”

from first principles. This choice is motivated by the desire to better

understand what is possible to achieve when adding privacy to

advertising when, largely, the concept feels like an oxymoron.

Ultimately, we find that this initial reaction is not far off: any

_useful_ behavioral advertising ecosystem must necessarily permit

advertisers to extract information about end users, regardless of

what privacy protections are in place. Leveraging the language

of ideal functionalities, we give an implementation-independent

modeling of privacy-preserving advertising that focuses on the

_minimal_ functionality required by the ecosystem.

Notably, our model departs from prior work in that it focuses

on the entire _end-to-end_ advertising pipeline, with a particular em
phasis on the ways that privacy-preserving targeting and privacy
preserving metrics interact with one another and form a feedback

loop. By seeing privacy-preserving advertising in this way, we are

able to identify real-world advertising use cases in which common

notions of privacy for targeting and metrics fail to compose sat
isfyingly, which undermines natural privacy guarantees for the

end-to-end system despite targeting and metrics protocols _indepen-_
_dently_ achieving reasonable standards for privacy. We emphasize

that this composition failure is not merely the result of specific, ill
designed protocols from early research; instead, it is fundamental

to the nature of targeted advertising itself.

Looking further: we also examine how to consider privacy for
mally in the context of advertising. While _perfect_ privacy may not be

possible for advertising, we observe that it is also not necessarily re
quired or even desirable. For instance, while contextual advertising

(where ads are targeted only to the context in which they will be dis
played, i.e., the website or article) can suffer from the same problems

as behavioral advertising in the worst case, it is still strongly pre
ferred as an alternative to behavioral advertising. While the privacy

provided by contextual advertising may be imperfect, it is likely

“good enough” [63, 99]. Hence, using the language of information

leakage alone makes it difficult to distinguish between advertising

systems that are widely considered “invasive” and those that are

not. We additionally observe that a narrow focus on building mech
anisms to regulate—without eliminating—information leakage, like

differential privacy [37], risk treating all types of information leak
age identically and missing the ways in which _people_ feel differently

about some sensitive information categories. As a result, we adopt

the framework of _attribute privacy_ [105] to evaluate privacy in the

context of advertising.

Relatedly, not all advertising campaigns have the same poten
tial for privacy harm, even if they do leak the same amount of

information. For this reason, we also employ the framework of

contextual integrity [71] to reason about the _sensitivity_ of the data

involved. This sentiment is captured, if not well-enforced, by cur
rent tech policy and legal regulations for ad targeting, but absent

from consideration in private metrics. This motivates a more holis
tic approach toward the design of privacy-preserving advertising

systems that reason carefully about the amount of information

revealed by metrics _and_ the sensitivity of that leakage.


## **1.1 Our Contributions**

In this work, we provide a careful accounting of the structure un
derpinning the advertising ecosystem. In doing so, we make the

following contributions:


- **A clean, formal, and flexible abstraction of the end-to-end**
**advertising process.** Our model, detailed in Section 4, makes

extensive use of parameterizing functions to ensure that our

abstraction is flexible enough to describe the behavior of the real
world advertising pipeline as well as ongoing proposals to make

it more private. We choose Canetti’s Universally Composable

(UC) security framework [1] [21] as the runtime for our modeling

in order to give structure to our analysis.

- **A formal illustration of the inherent tension between pri-**
**vacy and utility.** In Section 3, we highlight a gap between the

individual focus of current private advertising protocols and

the structure of advertising itself which considers _audiences_, or

groups of people. Leveraging our model, we concretize this gap

in Section 5 by providing lightweight minimum utility notions re
quired of each component of an advertising ecosystem, which we

use to prove an inherent incompatibility with the group privacy

notion, _attribute privacy_ [105]. Specifically, we prove that any
_useful_ advertising ecosystem must necessarily leak some infor
mation about its users—even when it employs strong, individual

privacy protections, such as differential privacy. We characterize

this leakage both theoretically and empirically in terms of the

difference in _sample complexity_ [22]—the campaign size required

for a private advertising ecosystem to leak the same information

as its non-private counterpart.

- **A refocus on normative privacy notations.** Having shown

that some leakage is inherent in advertising, in Section 6 we

advocate for rooting future discussions of privacy in data _sen-_
_sitivity_ as understood by end users, ad tech platforms, and reg
ulatory bodies. Data sensitivity is well-studied when it comes

to private ad targeting, but is largely ignored by private metrics

protocols, which focus exclusively on the _quantity_ of leakage.
We propose that by making metrics _targeting-aware_, protocols

could incorporate the idea of sensitivity and serve as a second

layer of enforcement–and accountability–for private advertising,

refocusing on _what_ information is revealed about users.

## **2 Background on (Private) Advertising** **2.1 The Advertising Ecosystem**


The language used within the advertising literature can be difficult

to parse for the unfamiliar reader. As such, we provide a brief

overview of digital advertising, focusing on the creation of metrics

data (we direct the reader to other surveys for more detail [77]).

We illustrate the life cycle of an advertising campaign in Figure 1.

A _campaign_, or collection of ads directed at a target audience, begins

at step 0 when the advertiser registers it with the ad network. In

this case, the advertiser is using their campaign to conduct an A/B

test, a common practice that we discuss in depth in Section 3, by

registering two ads: one ad (A) depicting a family with children and


1As we discuss in Section 4, the way in which we use this model is non-standard, as

we use it to prove _a lack of security_ .



451


Proceedings on Privacy Enhancing Technologies 2026(1) Hogan et al.


**Figure 1: An illustration of the advertising ecosystem, depicting the process of generating metrics on a behavioral advertising**
**campaign. See Section 2.1 for details.**



another ad (B) without children. In both cases the ads are directed

at an audience of “busy people looking for a convenient meal”.

Later, 1 when a user browses to a publisher website displaying

ads, that website will send a request 2 to the ad network containing

data on the site itself as well as an identifier for the user. The ad

network then runs a targeting model and a real-time bidding auction


3, incorporating data from the advertiser, in order to select which

on a different site) as a result of seeing this ad.

Engagement with an advertiser, such as making a purchase or

even adding items to a cart, is known as a _conversion_ . The ad network attributes 7 the user’s conversion to the _impression_, or ad

view, it believes was responsible. In this case, that was the most

recent impression, ignoring ads from unrelated campaigns. This

process is known as _attribution_, and attributing a conversion to the

most recent impression is a strategy called “last touch.”

Eventually, the ad network collects attribution data 8 from **all**

users who viewed ads in this campaign and uses it to compute

metrics 9 that the advertiser can use to determine which ad from

the campaign was more successful in driving purchases.

In more detail, these metrics largely correspond to a count of

how many conversions were attributed to each ad, and they are

what allow the advertiser to run its A/B test and refine their strat
egy for future campaigns to focus more effort on ad content that

drove higher engagement [14, 55]. For this example, ad A outper
formed ad B, so the advertiser will likely focus future advertising

spend towards parents. We go into more detail on the leakage from

advertising metrics and its potential for harm in Section 3.

## **2.2 Related Work**


While there is a rich history of academic research on privatizing

advertising [8, 13, 46, 48, 51, 52, 56, 62, 74, 83, 84, 90, 92, 106, 107],

the majority of this work has not considered the potential impact

of releasing metrics data (beyond the possibility of linking an in
dividual conversion report to the specific user who generated it).

Exceptions to this include AdVeil [83], Themis [74], CookieMonster

[90], and various industry proposals for privatizing metrics (Apple’s

PCM/PAM [5, 100], Google’s ARA [42], Meta/Mozilla’s IPA [73],

and a W3C standardization effort PPA [47]) that we discuss next.



Adveil [83] presents a protocol for the full advertising pipeline,

and it considers the fact that metrics reports can be revealing of per
sonal data even if they are not directly linkable to an individual user.

Themis [74] is an early industry proposal that uses a consortium

blockchain to provide transparency and accountability for metrics

data. It, again, provides unlinkability between users and their re
ports, but it does not have further privacy protections for metrics

data. CookieMonster [90] is a recent work out of the W3C Pri
vate Advertising Technology Community Group (PATCG), which is

working to standardize a private metrics protocol. CookieMonster

provides a full model and security analysis for differentially-private

ad metrics with emphasis on handling complexities in privacy loss

budgets. It grew out of earlier work on Interoperable Private At
tribution (IPA) [73] and is part of the work on Privacy Preserving

Attribution (PPA) [47]. Apple’s Private Attribution Measurement

(PAM) [100] and Google’s Attribution Reporting API (ARA) [42]

are both alternative, differential-privacy-based proposals—though

PAM was recently superseded by PPA.

To the best of our knowledge, ours is the first work to formally

model and prove the _presence_ of leakage for _all_ advertising systems,

rather than the absence of leakage for a specific system.

## **3 Defining Privacy for Advertising**


Privacy is a multifaceted [86] and contextually embedded [70] con
cept that does not permit a unified definition, so we first concretize

what we mean by privacy within advertising. We begin with a

_leakage_ -based notion; ideally, advertising systems that aim to preserve privacy should prevent _any_ information from leaking about

users. Emerging proposals for private advertising are rapidly mov
ing towards this “no-leakage” world by pushing more of the tar
geting logic to clients’ own devices (e.g., FLEDGE [78]). Such a

shift is a step in the right direction—away from the mass surveil
lance [16, 29, 82, 94, 103] that currently supplies personal data for

ad targeting. However, delivering relevant ads is only one step in

the advertising pipeline.

Advertisers also want metrics on how these ads perform. Ads

can be expensive, and performance metrics allow advertisers to

direct their spending to campaigns that drive a better return on ad

spend (ROAS) [77]. Yet, even very basic metrics, such as which ads

were delivered, violate our zero-leakage goals as, due to the nature



452


Making Sense of Private Advertising Proceedings on Privacy Enhancing Technologies 2026(1)



of targeting, the ads themselves are revealing of their audience

[25, 66]. Recognizing that metrics leakage could cause significant

harm, there has been a widespread effort [47, 90] to make the met
rics computed on advertisement performance differentially private,

limiting the amount of information contained about individuals.

However, as we demonstrate in this work, advertising requires

more than individual privacy in order to adequately protect the

information that users consider to be important.

We use this section to introduce metrics as a critical component

of the advertising ecosystem, outline why it renders perfect privacy

impossible for advertising, and argue that seemingly-natural fixes—

such as differentially-private metrics—fail to adequately mitigate

the privacy harms that can arise from advertising ecosystems.

## **3.1 Market Research as Information Leakage**


The insights that advertisers derive from metrics go far beyond

simple counts of how often advertisements are shown, and they

are used to conduct market research on how users engage with the

ads they are shown. By collecting metrics on the relative successes

of their current advertising campaigns, advertisers can refine the

content and target audience of future campaigns to focus their ad

spending on serving appealing content to the people who are most

likely to engage with it [41].

The most clear example of this type of market research is the A/B

test, a practice where advertisers can create two versions of an ad

and test which is preferred by their target audience or, conversely,

test which of two possible target audiences gets better results for

a given ad campaign. A/B tests are so commonplace that major

advertising platforms have built-in tools for advertisers to set up

their experiments. [2]


To illustrate how such tests are conducted, we revisit Figure 1

and consider an instant meal-kit company that wants to decide

whether to focus its ad spend toward parents with young children.

Such a company could set up its campaign in two main ways, testing

on the target audience or on the ad content:


(1) Create two different ads, both depicting someone in a rush using

the meal-kit to prepare a quick meal, but ad _𝐴_ features a toddler
and ad _𝐵_ does not.

(2) Show the generic meal-kit ad (i.e., one without any particular

features that suggest its relevance to parents) to two different

audiences, audience A being, e.g., “busy parents looking for a

convenient meal option” [3] while audience B removes the parent

feature, e.g., “people looking for a convenient meal option.”


No matter which A/B testing approach is leveraged, the result

is fundamentally the same: advertisers learn whether members of

their audience are more likely to be parents with young children

based on the relative performance of A and B. In case (1), this

follows from one of the core axioms of advertising: people are more

likely to engage with ads that are more relevant to them [57, 64].

By contrast, in case (2) the targeting algorithm will preferentially

show the ad it believes to be more relevant to the audience, i.e.,


[2See, for example, Facebook A/B Testing and Google A/B Testing.](https://www.facebook.com/business/ads/ab-testing/)

3While this may seem quite abstract, in practice, targeting on these types of audiences

is enabled with a combination of machine learning models, externally gathered data,

[and identification of lookalike audiences. See Criteo’s Audience Overview for more](https://help.criteo.com/kb/guide/en/about-audiences-0EJNOqUqYu/Steps/842036)

about audience generation practices.



if many of the audience members are parents, then ad A will be

shown more frequently.

Market research is an iterative process. Consider our earlier

campaign example of marketing ready-made meal kits: the first

iteration could test whether the audience of “busy people who don’t

cook” also tends to have the “new parent” feature. Supposing that

this turns out to be the case, the advertiser can then test whether

“busy new parents who don’t cook” tend to prefer “health-focused”

meals and so forth. Thus, this practice does not only reveal a “little

bit more” information about audiences, but can be used (over time)

to extract tremendous amounts of information about audiences.

Much market research is, like this example, relatively innocuous.

However, this same infrastructure can be (and is) used to learn

about _arbitrary_ topics. We see examples of this with researchers

leveraging these platforms to carry out their own research studies

[81]—studies that are suspiciously close to “human subject research”

that is generally expected to be under the close supervision of

institutional review boards. For instance, consider the study by

Chan et al. that uses advertising to assess whether conservatives

are likely to have stronger brand attachment [26]. Another study

used advertising to assess public perceptions of refugees, though it

did acknowledge the ethical considerations of the research [1].

The upshot is that market research—an inherently desired compo
nent within any advertising system—enables a level of data-mining

that goes far beyond improving the quality of advertising. How
ever, the existing discussion of privacy in advertising is centered

on _individual_ privacy, whereas the leakage we describe here is a
_group_ privacy harm.

## **3.2 Distributional Privacy for Advertising**


At first glance, this may seem like a natural place to utilize dif
ferential privacy (DP) [37], and indeed most proposals for private

advertising metrics systems use DP. However, simply privatizing

the aggregated metrics in this way is insufficient. Market research

involves inference over a target audience or _group_ of people, not

an individual user, and DP is intentionally designed to enable this

type of inference [38].

Unlike a typical research study, the selection process for adver
tising audiences is designed to ensure that their members are _not_

representative of the general population [28, 59]. Instead, audiences

will often overwhelmingly represent small, arcane minority groups

whose members may not even realize that such a grouping exists

[67]: examples of audience profiles include “receptive to emotional

messaging,” “rollercoaster romantics,” “heavy buyers of pregnancy

tests,” and “strugglers and strivers – credit reliant” [6].

Many of these audiences represent vulnerable populations and

allowing advertisers to extract arbitrary information about them

can be harmful even when it doesn’t permit linking this informa
tion to individuals. Manipulative advertising practices use these

inferences to tailor their messaging to the viewer, increasing its

effectiveness [11, 89, 104]. A common example of this practice is in

political advertising where ads are typically _microtargeted_ to spe
cific populations with the intent to influence their vote [60, 79, 108].

An additional challenge is that DP doesn’t provide protection

against an advertiser applying the group-level inferences made over

the audience to its individual members [81]. A common example



453


Proceedings on Privacy Enhancing Technologies 2026(1) Hogan et al.


for DP is that learning the group trend that “smoking causes cancer”

would also imply that any specific smoker is at risk of cancer. But

advertising takes this a step further due to “custom audiences”

that can be composed of specific, identifiable individuals such as

those on the advertiser’s mailing list. For these audiences, a better

analogy might be revealing that members of a specific sci-fi book

club have an unusually high rate of cancer. Unlike in the case of

smoking, this is not a global inference implying that sci-fi books

cause cancer, but is instead reflective of the health status of these

specific people. Revealing this type of inference—even with DP

guarantees—is likely counter to peoples’ expectation of privacy,

especially given the information asymmetry in advertising where

inferences are revealed to the advertisers, but not their audience.

For this reason, we instead employ _attribute privacy_ [105] to cap
ture the potential privacy harms from advertising market research.

**Figure 2: UC functionalities for advertising ecosystem.**

## **3.3 Attribute Privacy in the Advertising Context**



Attribute privacy, proposed by Zhang et al. [105], describes the

ability of an adversary to learn information about specific, sensi
tive attributes of a population given summary statistics about that

population. It defines sensitivity around the maximum contribution

that the _distribution_ of some sensitive feature in a population may

have on the output of statistics computed over that population.

In this section, we provide the formal definition of attribute

privacy, which is built on the pufferfish privacy framework [58]. [4]


Later, in Section 6, we provide some guidance on how attribute

privacy could be integrated into the advertising ecosystem as a

potential enforcement mechanism for user-focused privacy policies.


**Definition 3.1** (Dataset Attribute Privacy, Definition 3 from Zhang
et al. [105]) **.** Let ( _𝑋_ 1 _[𝑗][,𝑋]_ 2 _[𝑗][, . . .,𝑋]_ _𝑚_ _[𝑗]_ [)] [be] [a] [record] [with] _[ 𝑚]_ [attributes]
that is sampled from an unknown distribution D, and let _𝑋_ =

[ _𝑋_ 1 _, . . .,𝑋𝑚_ ] be a dataset of _𝑛_ records sampled i.i.d. from D where

_𝑋𝑖_ denotes the (column) vector containing values of the _𝑖_ th attribute
of every record. Let _𝐶_ ⊆[ _𝑚_ ] be the set of indices of sensitive
attributes, and for each _𝑖_ ∈ _𝐶_, let _𝑔𝑖_ ( _𝑋𝑖_ ) be a function with codomain
U _[𝑖]_ .
A mechanism M satisfies ( _𝜖,𝛿_ ) _-dataset attribute privacy_ if it is
( _𝜖,𝛿_ )-Pufferfish private for the following framework ( _𝑆,_ Q _,_ Θ):


Set of secrets: _𝑆_ = { _𝑠𝑎_ _[𝑖]_ [:][=] [ 1] [[] _[𝑔][𝑖]_ [(] _[𝑋][𝑖]_ [)] [∈U] _𝑎_ _[𝑖]_ []] [:][ U] _𝑎_ _[𝑖]_ [⊆U] _[𝑖][,𝑖]_ [∈] _[𝐶]_ [}][.]
Set of secret pairs: Q = {( _𝑠𝑎_ _[𝑖]_ _[,𝑠]_ _𝑏_ _[𝑖]_ [)] [∈] _[𝑆]_ [×] _[ 𝑆,𝑖]_ [∈] _[𝐶]_ [}][.]
Distribution: Θ is a set of possible distributions _𝜃_ over the dataset
_𝑋_ . For each possible distribution D over records, there exists a _𝜃_ D ∈ Θ that corresponds to the distribution over _𝑛_
i.i.d. samples from D.


To contextualize this definition in the advertising setting, con
sider the dataset _𝑋_ to be an advertising audience with _𝑛_ members,
each represented by a feature vector ( _𝑋_ 1 _[𝑗][,𝑋]_ 2 _[𝑗][, . . .,𝑋]_ _𝑚_ _[𝑗]_ [)] [of length] _[ 𝑚]_
indicating the attributes of the _𝑗_ _[𝑡ℎ]_ user. Some attributes will be

considered _sensitive_ and represented in _𝐶_ ⊆[ _𝑚_ ]. [5] Then:


- The secret pairs ( _𝑠𝑎_ _[𝑖]_ _[,𝑠]_ _𝑏_ _[𝑖]_ [)] [for] [a] [sensitive] [attribute] _[ 𝑖]_ [are] [possible]
realizations of some function _𝑔𝑖_ ( _𝑋𝑖_ ) over that sensitive attribute.


4For some background on Pufferfish privacy, see Section E.

5In Section 6, we employ the contextual integrity framework [71] to provide guidance

on how to decide which attributes might be sensitive in the context of advertising.



For advertising metrics, we can think of _𝑔𝑖_ () as computing the

fraction of the audience who possess the sensitive attribute.

- Θ is the set of possible distributions that could have generated

the audience shown in _𝑋_ . Each _𝜃_ is intended to capture possible

correlations across attributes.

Formally, the sensitivity of an output statistic _𝐹_ ( _𝑋_ ) over the

dataset _𝑋_ is computed as follows:



For advertising, we consider _𝐹_ ( _𝑋_ ) to be the metrics for an advertis
ing campaign (e.g., a count of ad clicks, conversions, or purchases).

Sensitivity captures the maximum impact on _𝐹_ ( _𝑋_ ), which occurs
for the pair of potential secrets ( _𝑠𝑎_ _[𝑖]_ _[,𝑠]_ _𝑏_ _[𝑖]_ [)][ in which all or none of the]

audience (respectively) have the sensitive attribute and for the _𝜃_

with the tightest correlation between this attribute and the conver
sion rate. In words: if possessing the sensitive attribute makes a

user significantly more (or less) likely to engage with an ad, then

varying the prevalence of this feature within the audience will have

a strong impact on reported number of conversions. For instance, in

our example A/B test from the previous section with an audience of

“busy people who don’t cook,” a toddler-focused ad is much more

strongly correlated with the sensitive attribute (parental status)

than an ad focused on the types of food contained in the meal kit.

We demonstrate later in Section 5 that a lack of attribute privacy

is inherent to advertising; i.e., any minimally useful ads ecosystem

will reveal some new information about its audiences. However, cur
rent instantiations and associated privacy definitions give very little

control over _what_ information leaks. In Section 6, we discuss the

concept of sensitivity in depth and argue that advertising requires

more than individual privacy in order to meet users’ expectations.

## **4 Modeling the Advertising Ecosystem**


In this section, we present our minimalist modeling of the advertis
ing ecosystem. Our modeling captures the _minimum_ information

leakage present in the advertising ecosystem, and it represents

an ecosystem that has been designed to eliminate all unintended

information flows back to advertisers. We perform this modeling

from the perspective of advertiser by having the advertiser set

target audiences and receive summary reports on ad display and



Δ _𝑖_ _𝐹_ = max max
_𝜃_ ∈Θ ( _𝑠𝑎_ _[𝑖]_ _,𝑠𝑏_ _[𝑖]_ [)∈Q]



��E[ _𝐹_ ( _𝑋_ )| _𝑠𝑎𝑖_ _[,𝜃]_ [] −] [E][[] _[𝐹]_ [(] _[𝑋]_ [)|] _[𝑠]_ _𝑏_ _[𝑖]_ _[,𝜃]_ []] �� _._ (1)



454


Making Sense of Private Advertising Proceedings on Privacy Enhancing Technologies 2026(1)



Ideal Functionality F _𝑆𝑜𝑐𝑖𝑒𝑡𝑦_ _[𝑛,]_ [D]


F _𝑆𝑜𝑐𝑖𝑒𝑡𝑦_ is parameterized with a number of individuals _𝑛_ and
a distribution over the user feature space D _._

**Initialize:** Upon receiving an init message from F _𝑈𝑠𝑒𝑟𝐷𝑎𝑡𝑎_

for _𝑈𝑠𝑒𝑟𝑖_ :


(1) if _𝑈𝑠𝑒𝑟𝑖_ does not already have recorded features, sample features for _𝑈𝑠𝑒𝑟𝑖_ from D and record.
(2) Send an init message to F _𝑈𝑠𝑒𝑟𝐷𝑎𝑡𝑎_ with _𝑈𝑠𝑒𝑟𝑖_ and its

recorded features.


**Figure 3:** F _𝑆𝑜𝑐𝑖𝑒𝑡𝑦_ _[𝑛,]_ [D] **[, our way of modeling the features provided]**
**to people in society.**


engagement. While other actors in the advertising ecosystem (e.g.,

publisher websites and ad networks) certainly have their own func
tionality goals, advertisers are the driving force behind the feedback

loop on user data through the advertising ecosystem. In Section 5

we will use this model to “prove _insecurity_,” i.e., show that useful ad
vertising ecosystems will necessarily leak information about their

users. Thus, if some future system provably instantiates our ideal

functionalities, that should not be misconstrued as a demonstration

that it is privacy-preserving in a normative sense. Instead, such a

system would have _at least_ the leakage we demonstrate here, and

may have substantially more.

## **4.1 Parameterizing Functions**


Our model makes heavy use of parameterizing functions when

specifying ideal functionalities. These parameterizing functions

mean that our model is _flexible_ enough to capture a wide variety

of potential advertising systems, including those that attempt to

preserve privacy, those that are widely understood to be privacy

invasive, or even systems that would make little sense to deploy

in practice. We note that it might be best to think of some of these

functions as being _stateful_ (e.g., if a privacy budget must be managed

over many queries); for simplicity, we do not explicitly manage

state for these functions, but observe that it is trivial to modify

our modeling to make them stateful. We briefly introduce these

parameterizing functions before presenting our formal model.


**The Targeting Filter** _𝜌_ **and Targeting Function** _𝑓_ t **.** We model the

decision to select which ad for a user _𝑢𝑠𝑒𝑟𝑖_ when they visit a website
as a two phase process: (1) from the set of _𝑢𝑠𝑒𝑟𝑖_ ’s features, a subset
features’ is extracted using a ( _deterministic_ ) filter function _𝜌_ and
is then (2) fed into an arbitrary ( _randomized_ ) selection function _𝑓_ t,

the output of which is an advertisement. The filter function _𝜌_ can

be thought of as a policy that limits the type of information that the

targeting logic _𝑓_ t is allowed to access. Real-world instantiations of

_𝜌_ could model: (1) intentional restrictions such as Google’s Topics

API [43] and (2) unintentional inaccuracies in targeting profiles.

Then, _𝑓_ t could embed any “secret sauce” used by the advertising

network to select the most effective advertisement to match to a

user, including an opaque machine learning model or even one



of the academic proposals for private ad targeting and auctions

[106, 107].


**The Browsing Function** _𝑓_ b **and Engagement Function** _𝑓_ e **.** We

make use of two (randomized) parameterizing functions, _𝑓_ b and
_𝑓_ e in order to capture _human behavior_ in our model. Specifically,

_𝑓_ b decides which website a user _𝑈𝑠𝑒𝑟𝑖_ will visit, and _𝑓_ e decides

how a user _𝑈𝑠𝑒𝑟𝑖_ will interact when presented with a particular

advertisement (e.g., will they generate a “conversion,” by purchasing

the advertised product). These are best thought of as “black boxes”

that need not be opened in order to understand the end-to-end

functioning of the system.


**The Attribution Function** _𝑓_ a **.** Within the advertising ecosystem,
each _conversion_ event must be _attributed_ to an advertisement im
pression (see Section 2). The attribution function _𝑓_ a performs the

logic of this attribution. For example, a common attribution func
tion is “last-touch attribution,” where the most recent impression

prior to a conversion receives all the “credit” for the conversion. In

the name of generality, the parameterizing attribution function _𝑓_ a

takes in a set of impressions (along with the context in which the im
pression occurred) and allocates scores to each of these impressions

according to arbitrary logic. In practice, _𝑓_ a could be instantiated by

the Privacy Preserving Attribution protocol [47].


**The Reporting Function** _𝑓_ r **.** Advertisers learn about the perfor
mance of various advertisements within a campaign by generating

a report. The exact nature of how this report is compiled from

the attribution scores is system specific, but we encourage readers

who want a concrete example to think about the report as simply a

histogram of advertisement performance (i.e., a measure of how ef
ficiently advertisement impressions became conversions). In many

of the emerging private advertising system proposals [5, 42, 47, 73],

report generation is done with differential privacy. We capture

this process generically with the _𝑓_ r parameterizing function, which

could be instantiated by any of these proposals or by some future

protocol with an alternative privacy mechanism.

## **4.2 Ideal Advertising Functionalities**


Before providing an overview of our model, we first introduce

definitions for an ad and an audience (we represent the latter using

_feature vectors_ in this work).


**Definition 4.1** (Ad) **.** An advertisement ad = { _𝑥_ 1 _, ...,𝑥ℓ_ } _,𝑥𝑖_ ∈{0 _,_ 1}

is a binary vector of length _ℓ_ . Each index in the vector represents a

particular (implicit) quality the media for that advertisement could

encode. When a particular index is 1, that means the feature is

present in the media. Importantly, we use this formalism to describe

a piece of media directly, rather than allowing an advertiser to

present media and then choose a binary vector associated with that

media; in this way, we assume that it is _impossible_ for an advertiser

to lie about the features of an ad.


**Definition 4.2** (Audience) **.** An audience = { _𝑥_ 1 _, ...,𝑥ℓ_ } _,𝑥𝑖_ ∈{0 _,_ 1}

is also a binary vector of length _ℓ_ . Each index in this vector encodes

an attribute that members of the audiences should have. We assume

that the meanings of indices for advertisements and audiences are

consistent with one another—that is, the _𝑖_ [th] element of each encodes

the same feature.



455


Proceedings on Privacy Enhancing Technologies 2026(1) Hogan et al.


Ideal Functionality F _𝑈𝑠𝑒𝑟𝐷𝑎𝑡𝑎_


F _𝑈𝑠𝑒𝑟𝐷𝑎𝑡𝑎_ has a set of users { _𝑈𝑠𝑒𝑟_ 0 _,𝑈𝑠𝑒𝑟_ 1 _, ...,𝑈𝑠𝑒𝑟𝑛_ } each with a set of features (specified by F _𝑆𝑜𝑐𝑖𝑒𝑡𝑦_ _[𝑛,]_ [D] [) and a][ browsing-history][. Additionally,]
for each _𝑈𝑠𝑒𝑟𝑖,_ F _𝑈𝑠𝑒𝑟𝐷𝑎𝑡𝑎_ also maintains three indices into their browsing-history: targeting _𝑖_, engagement _𝑖_, and attribution _𝑖_ .

**Browsing:** Upon receiving a browsing message from F _𝐸𝑛𝑔𝑎𝑔𝑒𝑚𝑒𝑛𝑡_ _[𝑓]_ [b] _[,𝑓]_ [e] [for] _[ 𝑈𝑠𝑒𝑟][𝑖]_ [:]

(1) If _𝑈𝑠𝑒𝑟𝑖_ has no features, send an init message to F _𝑆𝑜𝑐𝑖𝑒𝑡𝑦_ _[𝑛,]_ [D] [and record the response. Set][ targeting] _[𝑖]_ [,][ engagement] _[𝑖]_ [, and][ attribution] _[𝑖]_ [to][ 0][.]

(2) Send _𝑈𝑠𝑒𝑟𝑖_ ’s identity, features, and full browsing-history to F _𝐸𝑛𝑔𝑎𝑔𝑒𝑚𝑒𝑛𝑡_ _[𝑓]_ [b] _[,𝑓]_ [e] [.]

(3) Upon receiving a response from F _𝐸𝑛𝑔𝑎𝑔𝑒𝑚𝑒𝑛𝑡_ _[𝑓]_ [b] _[,𝑓]_ [e] [with] [site][,] [append] [site] [to] [the] [browsing-history] [for] _[ 𝑈𝑠𝑒𝑟][𝑖]_ [.] [Then,] [send] [ok] [message] [to]

F _𝐸𝑛𝑔𝑎𝑔𝑒𝑚𝑒𝑛𝑡_ _[𝑓]_ [b] _[,𝑓]_ [e] [.]


**Ad Targeting:** Upon receiving a target message from F _𝑇𝑎𝑟𝑔𝑒𝑡𝑖𝑛𝑔_ _[𝑓]_ [t] _[,𝜌]_ [for] _[ 𝑈𝑠𝑒𝑟][𝑖]_ [:]

(1) If targeting _𝑖_ = null (i.e., browsing has not been called for this user) respond fail to F _𝑇𝑎𝑟𝑔𝑒𝑡𝑖𝑛𝑔_ _[𝑓]_ [t] _[,𝜌]_ [.]

(2) Send _𝑈𝑠𝑒𝑟𝑖_ ’s identity, features, and browsing-history[0 : targeting _𝑖_ ] to F _𝑇𝑎𝑟𝑔𝑒𝑡𝑖𝑛𝑔_ _[𝑓]_ [t] _[,𝜌]_ [.]

(3) Upon receiving a response from F _𝑇𝑎𝑟𝑔𝑒𝑡𝑖𝑛𝑔_ _[𝑓]_ [t] _[,𝜌]_ [for] _[ 𝑈𝑠𝑒𝑟][𝑖]_ [of the form (][site][,][ ad][), if][ browsing-history][[][targeting] _[𝑖]_ []] [contains an matching entry]

site, then overwrite the entry with (site, ad) and increment targeting _𝑖_ . Then, send ok to F _𝑇𝑎𝑟𝑔𝑒𝑡𝑖𝑛𝑔_ _[𝑓]_ [t] _[,𝜌]_ [.]

**Ad engagement:** Upon receiving an engagement message from F _𝐸𝑛𝑔𝑎𝑔𝑒𝑚𝑒𝑛𝑡_ _[𝑓]_ [b] _[,𝑓]_ [e] [for] _[ 𝑈𝑠𝑒𝑟][𝑖]_ [:]

(1) If engagement _𝑖_ = targeting _𝑖_ (i.e., engagement decisions were made for all impressions), respond fail to F _𝐸𝑛𝑔𝑎𝑔𝑒𝑚𝑒𝑛𝑡_ _[𝑓]_ [b] _[,𝑓]_ [e] [.]

(2) Respond to F _𝐸𝑛𝑔𝑎𝑔𝑒𝑚𝑒𝑛𝑡_ _[𝑓]_ [b] _[,𝑓]_ [e] [with] _[ 𝑈𝑠𝑒𝑟][𝑖]_ [’s][ features][ and the tuple][ browsing-history][[][engagement] _[𝑖]_ []] [=] [(][site] _[,]_ [ ad][)] _[.]_

(3) Upon receiving a response from F _𝐸𝑛𝑔𝑎𝑔𝑒𝑚𝑒𝑛𝑡_ _[𝑓]_ [b] _[,𝑓]_ [e] [with] [ad] [and] [conversion] [for] _[𝑈𝑠𝑒𝑟][𝑖]_ [,] [overwrite] [browsing-history][[][engagement] _[𝑖]_ []] [to] [be]

(site _,_ ad _,_ conversion) and increment engagement _𝑖_ _._ Then, send ok message to F _𝐸𝑛𝑔𝑎𝑔𝑒𝑚𝑒𝑛𝑡_ _[𝑓]_ [b] _[,𝑓]_ [e] [.]


**Attribution:** Upon receiving a attribute message from F _𝑀𝑒𝑡𝑟𝑖𝑐𝑠_ _[𝑓]_ [a] _[,𝑓]_ [r] [for] _[ 𝑈𝑠𝑒𝑟][𝑖]_ [:]
(1) Let _𝑗_ = attribution _𝑖_ _._ Starting with _𝑗_, find the first entry of browsing-history with a tuple (site _,_ ad _,_ conversion) such that conversion
is non-None. Set attribution _𝑖_ to be the index of this entry. If no such index exists, respond fail to F _𝑀𝑒𝑡𝑟𝑖𝑐𝑠_ _[𝑓]_ [a] _[,𝑓]_ [r] [instead.]
(2) Send an attribution message to F _𝑀𝑒𝑡𝑟𝑖𝑐𝑠_ for _𝑈𝑠𝑒𝑟𝑖_ with browsing-history[ _𝑗_ : attribution _𝑖_ ] _._


**Figure 4:** F _𝑈𝑠𝑒𝑟𝐷𝑎𝑡𝑎_ **holds data about each user and is responsible to shuttling information between** F _𝑇𝑎𝑟𝑔𝑒𝑡𝑖𝑛𝑔,_ F _𝐸𝑛𝑔𝑎𝑔𝑒𝑚𝑒𝑛𝑡_ _,_ **and**
F _𝑀𝑒𝑡𝑟𝑖𝑐𝑠_ **. Handles user features along with all data related to ad impressions and conversions.**



**Model overview.** We give a high-level depiction of our model in

Figure 2. Namely, our model consists of five main ideal functionalities: F _𝑆𝑜𝑐𝑖𝑒𝑡𝑦_ _[𝑛,]_ [D] [,][ F] _[𝑈𝑠𝑒𝑟𝐷𝑎𝑡𝑎]_ [,][ F] _𝑇𝑎𝑟𝑔𝑒𝑡𝑖𝑛𝑔_ _[ 𝑓]_ [t] _[,𝜌]_ _[,]_ [ F] _𝐸𝑛𝑔𝑎𝑔𝑒𝑚𝑒𝑛𝑡_ _[ 𝑓]_ [b] _[,𝑓]_ [e] _[,]_ [ and][ F] _𝑀𝑒𝑡𝑟𝑖𝑐𝑠_ _[ 𝑓]_ [a] _[,𝑓]_ [r] _[.]_ [ In]

Figure 3, F _𝑆𝑜𝑐𝑖𝑒𝑡𝑦_ _[𝑛,]_ [D] [is responsible for sampling the features for each]
of the _𝑛_ users in the system from some distribution D _._ Importantly,

this means that the exact features for each user is hidden from the

environment—although the distribution D may be known to the
environment. In Figure 4, F _𝑈𝑠𝑒𝑟𝐷𝑎𝑡𝑎_ is a subroutine that serves as

the shared data infrastructure of the entire system, including hold
ing each user’s features and information about their interactions

with the advertising system. We note that F _𝑈𝑠𝑒𝑟𝐷𝑎𝑡𝑎_ might be im
plemented in a distributed manner, such that different elements of

the data may be held by different real-world computational parties.
In Figures 5 to 7, F _𝑇𝑎𝑟𝑔𝑒𝑡𝑖𝑛𝑔_ _[𝑓]_ [t] _[,𝜌]_ [,][ F] _𝐸𝑛𝑔𝑎𝑔𝑒𝑚𝑒𝑛𝑡_ _[ 𝑓]_ [b] _[,𝑓]_ [e] [, and][ F] _𝑀𝑒𝑡𝑟𝑖𝑐𝑠_ _[ 𝑓]_ [a] _[,𝑓]_ [r] [make up the]

core of the advertising ecosystem. Concretely, F _𝑇𝑎𝑟𝑔𝑒𝑡𝑖𝑛𝑔_ _[𝑓]_ [t] _[,𝜌]_ [is respon-]

sible for choosing advertisements to deliver to users, F _𝐸𝑛𝑔𝑎𝑔𝑒𝑚𝑒𝑛𝑡_ _[𝑓]_ [b] _[,𝑓]_ [e]

is responsible for determining the websites that a user visits and

how a user will interact with advertisements on those websites,
and F _𝑀𝑒𝑡𝑟𝑖𝑐𝑠_ _[𝑓]_ [a] _[,𝑓]_ [r] [is responsible for attributing conversion events and]

reporting on the performance of advertisements.



**Model flow details.** Next, we illustrate how these functionalities

work and provide a detailed description of the way data flows

through the system. We note that some of our functionalities also

allow for interactions to occur in a different order.


**(1)** **Populating** **user** **features:** The features associated with
each user are set up on demand. Specifically, F _𝑆𝑜𝑐𝑖𝑒𝑡𝑦_ _[𝑛,]_ [D] [is set up with]

a total number of individuals _𝑛_ that it will create and a distribu
tion from which each individual’s features will be sampled. The

environment does not need to explicitly initiate this sampling process, as F _𝑆𝑜𝑐𝑖𝑒𝑡𝑦_ _[𝑛,]_ [D] [will perform this “just in time” whenever][ F] _[𝑈𝑠𝑒𝑟𝐷𝑎𝑡𝑎]_

encounters a user with no recorded features.


**(2)** **Registering Ad Campaigns:** When an advertiser wants to
send an advertisement, they begin by sending a **Register Cam-**
**paign** message to F _𝑇𝑎𝑟𝑔𝑒𝑡𝑖𝑛𝑔_ _[𝑓]_ [t] _[,𝜌]_ [(Figure 5) that specifies the explicit]
target audience to which they want to advertisements to be shown
as well as the features of the advertisements {ad1 _, . . .,_ ad _𝑘_ } in the

campaign (e.g., embedded within the visual media). We emphasize

that the modeling is done such that the advertiser cannot “lie” about

the semantic content of the advertisement—the feature vector _is_

the advertisement.



456


Making Sense of Private Advertising Proceedings on Privacy Enhancing Technologies 2026(1)



Ideal Functionality F _𝑇𝑎𝑟𝑔𝑒𝑡𝑖𝑛𝑔_ _[𝑓]_ [t] _[,𝜌]_


F _𝑇𝑎𝑟𝑔𝑒𝑡𝑖𝑛𝑔_ is parameterized by a _stateful_, randomized func
tion _𝑓_ t and a filtering function _𝜌_ . It also maintains a set
active-campaigns.

**Register** **Campaign:** Upon receiving a campaign :
(audience _,_ {ad1 _, . . .,_ ad _𝑘_ }) message from the _𝐸𝑛𝑣_ :

(1) Add campaign to active-campaigns.
(2) Send an ok message to the _𝐸𝑛𝑣_ .

**Target Ad:** Upon receiving an ad message from _𝐸𝑛𝑣_ for _𝑈𝑠𝑒𝑟𝑖_ :

(1) Send a target message to F _𝑈𝑠𝑒𝑟𝐷𝑎𝑡𝑎_ for _𝑈𝑠𝑒𝑟𝑖_ .
(2) Upon receiving a response from F _𝑈𝑠𝑒𝑟𝐷𝑎𝑡𝑎_ with
features and browsing-history for _𝑈𝑠𝑒𝑟𝑖_ :
(a) run features’ ← _𝜌_ ( _𝑈𝑠𝑒𝑟𝑖,_ features) to obtain
features’ ⊆ features
(b) Extract site from the final element of
browsing-history.
(3) Compute ad ← _𝑓_ t(active-campaigns, features’,
site).
(4) Send F _𝑈𝑠𝑒𝑟𝐷𝑎𝑡𝑎_ a message with the identifier _𝑈𝑠𝑒𝑟𝑖_ and
a tuple of the form (site _,_ ad).
(5) Upon receiving ok or fail from F _𝑈𝑠𝑒𝑟𝐷𝑎𝑡𝑎_, send ok to

the _𝐸𝑛𝑣_ .


**Figure 5: Targeting functionality** F _𝑇𝑎𝑟𝑔𝑒𝑡𝑖𝑛𝑔_


**(3)** **Initiating browsing:** The environment prompts the user to
browse a website by calling the **Browsing** interface of F _𝐸𝑛𝑔𝑎𝑔𝑒𝑚𝑒𝑛𝑡_ _[𝑓]_ [b] _[,𝑓]_ [e] _[.]_

Note that the environment does not know the specific features

of any given user, so we don’t have the environment specify the

website directly. Rather, we use _𝑓_ b to choose the website that the user
visits, possibly based on the user’s features. Specifically, F _𝐸𝑛𝑔𝑎𝑔𝑒𝑚𝑒𝑛𝑡_ _[𝑓]_ [b] _[,𝑓]_ [e]
requests _𝑈𝑠𝑒𝑟𝑖_ ’s features from F _𝑈𝑠𝑒𝑟𝐷𝑎𝑡𝑎_ and obtains the site using

_𝑓_ b, which is defined over the features of the user and their previous
browsing history. Then, F _𝐸𝑛𝑔𝑎𝑔𝑒𝑚𝑒𝑛𝑡_ _[𝑓]_ [b] _[,𝑓]_ [e] [informs][ F] _[𝑈𝑠𝑒𝑟𝐷𝑎𝑡𝑎]_ [that] _[ 𝑈𝑠𝑒𝑟][𝑖]_
has visited site.

**(4)** **Advertisement** **Targeting** **and** **Delivery:** To model the

delivery of an advertisement to a user that has been prompted to

visit a website, the environment uses the **Target Ad** interface of
F _𝑇𝑎𝑟𝑔𝑒𝑡𝑖𝑛𝑔_ _[𝑓]_ [t] _[,𝜌]_ _[.]_ [ This triggers a][ target][ message to][ F] _[𝑈𝑠𝑒𝑟𝐷𝑎𝑡𝑎]_ [in order to]

obtain the necessary information about the user and the context
(i.e., site) in which the ad will be displayed. Next, F _𝑇𝑎𝑟𝑔𝑒𝑡𝑖𝑛𝑔_ _[𝑓]_ [t] _[,𝜌]_ [uses]

_𝜌_ and _𝑓_ t to select the ad that will be shown to the user. Note that
the input to _𝑓_ t should operate over the the audience associated

with the advertisements. The chosen advertisement is then sent to

F _𝑈𝑠𝑒𝑟𝐷𝑎𝑡𝑎_ to be stored.

**(5)** **User** **Engagement:** After the user has viewed the advertisement (i.e., F _𝑈𝑠𝑒𝑟𝐷𝑎𝑡𝑎_ holds a tuple containing a site and an
ad), the environment triggers possible user engagement using the
**Ad engagement** interface of F _𝐸𝑛𝑔𝑎𝑔𝑒𝑚𝑒𝑛𝑡_ _[𝑓]_ [b] _[,𝑓]_ [e] _[.]_ [ In response,][ F] _𝐸𝑛𝑔𝑎𝑔𝑒𝑚𝑒𝑛𝑡_ _[ 𝑓]_ [b] _[,𝑓]_ [e]
retrieves the necessary information from F _𝑈𝑠𝑒𝑟𝐷𝑎𝑡𝑎_ and, using the



Ideal Functionality F _𝐸𝑛𝑔𝑎𝑔𝑒𝑚𝑒𝑛𝑡_ _[𝑓]_ [b] _[,𝑓]_ [e]


F _𝐸𝑛𝑔𝑎𝑔𝑒𝑚𝑒𝑛𝑡_ is parameterized by _𝑓_ b that selects a site for a user

to visit and _𝑓_ e that determines how a user will interact with

an advertisement.

**Browsing:** Upon receiving an browsing message for _𝑈𝑠𝑒𝑟𝑖_

from _𝐸𝑛𝑣_ :


(1) Send a browsing message to F _𝑈𝑠𝑒𝑟𝐷𝑎𝑡𝑎_ for _𝑈𝑠𝑒𝑟𝑖_ .
(2) Upon receiving a response from F _𝑈𝑠𝑒𝑟𝐷𝑎𝑡𝑎_ for _𝑈𝑠𝑒𝑟𝑖_
with features, and full browsing-history, generate
site ← _𝑓_ b (features _,_ browsing-history) _._
(3) Send _𝑠𝑖𝑡𝑒_ to F _𝑈𝑠𝑒𝑟𝐷𝑎𝑡𝑎_ for _𝑈𝑠𝑒𝑟𝑖_ .
(4) Upon receiving ok from F _𝑈𝑠𝑒𝑟𝐷𝑎𝑡𝑎_, send ok message

to _𝐸𝑛𝑣_ .

**Ad Engagement:** Upon receiving an engagement message

for _𝑈𝑠𝑒𝑟𝑖_ from _𝐸𝑛𝑣_ :

(1) Send an engagement message to F _𝑈𝑠𝑒𝑟𝐷𝑎𝑡𝑎_ for _𝑈𝑠𝑒𝑟𝑖_ .
(2) Upon receiving a response from F _𝑈𝑠𝑒𝑟𝐷𝑎𝑡𝑎_ with
_𝑈𝑠𝑒𝑟𝑖_ ’s features and a tuple (site _,_ ad) _,_ generate
conversion ← _𝑓_ e (features _,_ site _,_ ad). Note that
conversion may be None.
(3) Respond to F _𝑈𝑠𝑒𝑟𝐷𝑎𝑡𝑎_ with _𝑈𝑠𝑒𝑟𝑖_, ad, and conversion.
(4) Upon receiving ok or fail from F _𝑈𝑠𝑒𝑟𝐷𝑎𝑡𝑎_, send ok

message to _𝐸𝑛𝑣_ .


**Figure 6: Engagement functionality** F _𝐸𝑛𝑔𝑎𝑔𝑒𝑚𝑒𝑛𝑡_


parameterizing function _𝑓_ e _,_ determines if the user turns generates

a conversion on that impression. Note that the input to _𝑓_ e is the
features associated with the advertising media ad (as the user actually sees the _media_, not the target audience). The results of this
determination are then stored back in F _𝑈𝑠𝑒𝑟𝐷𝑎𝑡𝑎_ .

**(6)** **Attribution:** Before any metrics information can be provided, F _𝑀𝑒𝑡𝑟𝑖𝑐𝑠_ _[𝑓]_ [a] _[,𝑓]_ [r] [must first attribute each conversion event to at least]
one impression. The environment prompts this through the **At-**
**tribute** interface of F _𝑀𝑒𝑡𝑟𝑖𝑐𝑠_ _[𝑓]_ [a] _[,𝑓]_ [r] _[.]_ [ When invoked in this way,][ F] _𝑀𝑒𝑡𝑟𝑖𝑐𝑠_ _[ 𝑓]_ [a] _[,𝑓]_ [r]
calls to F _𝑈𝑠𝑒𝑟𝐷𝑎𝑡𝑎_ and retrieves the user’s conversion history. Then,
F _𝑀𝑒𝑡𝑟𝑖𝑐𝑠_ _[𝑓]_ [a] _[,𝑓]_ [r] [updates the “scores” of each ad based on the output] _[ 𝑓]_ [a] _[.]_ [ It]

is easiest to think of this step as attributing the full “credit” for the

conversion to the last impression.


**(7)** **Report Creation:** Finally, the environment (as the adver
tiser) requests a report on the performance of its campaign. To

do this, the environment invokes the **Generate Report** interface
of F _𝑀𝑒𝑡𝑟𝑖𝑐𝑠_ _[𝑓]_ [a] _[,𝑓]_ [r] [,] [specifying] [a] [campaign] [(i.e.,] [a] [set] [of] [advertisements).]

These are then transformed into a report by the _𝑓_ r function, which

is also responsible for adding noise or any other privacy protection

mechanism.



457


Proceedings on Privacy Enhancing Technologies 2026(1) Hogan et al.



Ideal Functionality F _𝑀𝑒𝑡𝑟𝑖𝑐𝑠_ _[𝑓]_ [a] _[,𝑓]_ [r]


F _𝑀𝑒𝑡𝑟𝑖𝑐𝑠_ is parameterized by an attribution function _𝑓_ a and report generation function _𝑓_ r. Additionally, F _𝑀𝑒𝑡𝑟𝑖𝑐𝑠_ maintains
an updatable map ad-scores.

**Attribute:** Upon receiving an attribute message from _𝐸𝑛𝑣_

for _𝑈𝑠𝑒𝑟𝑖_ :

(1) Send a attribute message to F _𝑈𝑠𝑒𝑟𝐷𝑎𝑡𝑎_ for _𝑈𝑠𝑒𝑟𝑖_ .
(2) Upon receiving a response from F _𝑈𝑠𝑒𝑟𝐷𝑎𝑡𝑎_
for _𝑈𝑠𝑒𝑟𝑖_ with list browsing-history,
run [(ad1 _,_ score1) _,_ (ad2 _,_ score2) _, ..._ ] ←
_𝑓_ a (browsing-history) _._
(3) For each ad _𝑖_ in the output, add score _𝑖_ to the entry for
ad _𝑖_ in ad-scores.
(4) Send an ok message to _𝐸𝑛𝑣_ .

**Generate Report:** Upon receiving a Report message from
_𝐸𝑛𝑣_ for a campaign : (audience _,_ {ad1 _, . . .,_ ad _𝑘_ }) _,_

(1) Let ad-scores|campaign be a map that is a subset of
ad-scores such that


ad-scores|campaign =


{(ad _𝑖,_ score _𝑖_ ) ∈ ad-scores | ad _𝑖_ ∈{ad1 _, . . .,_ ad _𝑘_ }} _._


(2) Generate report ← _𝑓_ r (ad-scores|campaign).
(3) Respond to _𝐸𝑛𝑣_ with a message containing report.


**Figure 7: Metrics functionality** F _𝑀𝑒𝑡𝑟𝑖𝑐𝑠_

## **5 Inherent Tension Between Privacy and** **Usefulness**


Now that we have introduced our abstract modeling of the adver
tising ecosystem, in this section we formalize two key concepts: (i)

what does it mean for this ecosystem to be “useful” (what is the

minimal functionality we need from our parameterizing functions)

and (ii) what does it mean to add privacy to this ecosystem. We use

this formalism to show, analytically and empirically, that privacy

and utility are inherently in tension.

## **5.1 Defining Utility**


**active-ads.** In order to make the notation more direct, we define
a set active-ads that represents the set of advertisements from
which targeting may choose. Specifically, for any active-campaigns,
let active-ads be defined as follows:


active-ads = {(audience _,_ ad) |


{audience _,_ ad-set} ∈ active-campaigns _,_ ad ∈ ad-set} _._


**Measuring closeness.** We also require a concept of _relevancy_ to

capture the idea that behavioral advertising is intended to show

users ads that are relevant, or closely matched, to their interests and

demographics. We represent this with a _𝑐𝑙𝑜𝑠𝑒_ _metric_ that takes as

input two (binary) feature vectors and outputs a score that increases

as the distance between the inputs shrinks.



**Targeting.** We begin with our utility function for targeting. Specif
ically, a useful targeting system should be one that delivers ads that

are more relevant to people with higher probability. We formalize

this notion by saying that the probability that an one advertisement

is chosen over another is proportional to the difference in _𝑐𝑙𝑜𝑠𝑒_

between the targeting audience and the user’s features. [6]


**Definition** **5.1** (Targeting Utility) **.** A targeting function _𝑓_ t is _𝛼_ _useful_ with respect to a distance measurement _𝑐𝑙𝑜𝑠𝑒_ and filter function _𝜌_ if, given inputs active-campaigns, features, and site, for
all (audience1 _,_ ad1) _,_ (audience2 _,_ ad2) ∈ active-ads _,_ if


_𝑐𝑙𝑜𝑠𝑒_ (audience1 _,_ features)−


_𝑐𝑙𝑜𝑠𝑒_ (audience2 _,_ features) = Δ _,_ then,


Pr [ad1 ← _𝑓_ t (active-campaigns _, 𝜌_ (features) _,_ site)] −

Pr [ad2 ← _𝑓_ t (active-campaigns _, 𝜌_ (features) _,_ site)] ≥ _𝛼_ ·Δ _._


**Engagement.** A foundational assumption of advertising is that

individuals are more likely to engage with advertisements that are

more “like them.” We formalize this idea using a closeness metric,

similar to the one in Definition 5.1 for targeting utility.


**Definition 5.2** (Engagement Utility) **.** We say that an engagement
function _𝑓_ e is _𝛼_ - _useful_ with respect to a distance measurement _𝑐𝑙𝑜𝑠𝑒_
if for any set of user features features, website site, pair of advertisements (ad1 _,_ ad2), and non-None conversion event conversion:


if _𝑐𝑙𝑜𝑠𝑒_ (ad1 _,_ features) − _𝑐𝑙𝑜𝑠𝑒_ (ad2 _,_ features) = Δ _,_


then Pr [conversion ← _𝑓_ e (features _,_ site _,_ ad1)]

   - Pr [conversion ← _𝑓_ e (features _,_ site _,_ ad2)] ≥ _𝛼_   - Δ _._


**Attribution.** Attribution is considered useful if it is more likely to

attribute a conversion to the impression that generated it than an

unrelated impression. For the purposes of our analysis, we model
utility of F _𝑀𝑒𝑡𝑟𝑖𝑐𝑠_ _[𝑓]_ [a] _[,𝑓]_ [r] [relative] [to] [the] [ground] [truth] [as] [generated] [by]

F _𝐸𝑛𝑔𝑎𝑔𝑒𝑚𝑒𝑛𝑡_ _[𝑓]_ [b] _[,𝑓]_ [e] [. One of the limitations of our model is that][ F] _𝐸𝑛𝑔𝑎𝑔𝑒𝑚𝑒𝑛𝑡_ _[ 𝑓]_ [b] _[,𝑓]_ [e]

does not model cases where many impressions contribute to a single

conversion event. So, while our attribution functionality is generic

and can handle multi-touch attribution, the ground truth for this

analysis is that a _single_ ad is responsible for each conversion.


**Definition** **5.3** (Attribution Utility) **.** An attribution function _𝑓_ a
is _𝛼_ - _useful_ with respect to an engagement function _𝑓_ e if for any
feature vector features, any pair of advertisements (ad1 _,_ ad2) and
associated websites site, and any non-None conversion event
conversion:


if Pr [conversion ← _𝑓_ e (features _,_ site _,_ ad1)] −

Pr [conversion ← _𝑓_ e (features _,_ site _,_ ad2)] = Δ _,_


then Pr [ _𝑠𝑐𝑜𝑟𝑒𝑠_ [ad1] _> 𝑠𝑐𝑜𝑟𝑒𝑠_ [ad2] |


_𝑠𝑐𝑜𝑟𝑒𝑠_ ← _𝑓_ a (conversion _,_ browsing-history)] ≥ _𝛼_    - Δ


if browsing-history contains both ad1 and ad2.


**Metrics.** Metrics is considered useful if it permits statistical tests

to be conducted on the results. That is, if some test, such as an


6In practice, _close_ should also take in site as an input. However, since this context is,

in theory, just a coarse-grained view into a user’s features, we ignore it in order to

simplify our analysis.



458


Making Sense of Private Advertising Proceedings on Privacy Enhancing Technologies 2026(1)



A/B test, could be conducted on the raw attribution data, it should

still be possible to conduct this test on the aggregated and possibly

noisy version of this data output by metrics. That is, the utility of

metrics is defined based on what the advertiser intended to do with

the attribution data.


**Definition 5.4** (Metrics Utility Preserving) **.** For all _ℎ_, we say that a
randomized metrics report generation function _𝑓_ r : D _[ℎ]_ →D _[ℎ]_ is _𝛼-_
_utility-preserving_ with respect to a (possibly randomized) processing
function _𝑓𝑠_ : D _[ℎ]_ →{0 _,_ 1} if for all _𝑑_ [ˆ] = { _𝑑_ 1 _, ...,𝑑ℎ_ } ∈D _[ℎ]_ _,_


|Pr[ _𝑓𝑠_ ( _𝑑_ [ˆ] ) = 1] − Pr[ _𝑓𝑠_ ({ _𝑓_ r ( _𝑑_ [ˆ] )}) = 1]| _< 𝛼,_


where the probabilities are over the randomness of _𝑓_ r and _𝑓𝑠_ . [7]

## **5.2 Formal Statement**


In this section, we state and prove our formal result: there is an

innate tension between preventing leakage and preserving utility

in an advertising ecosystem.


**Theorem** **1.** Any ads ecosystem composed of instantiations of
F _𝑇𝑎𝑟𝑔𝑒𝑡𝑖𝑛𝑔_ _[𝑓]_ [t] _[,𝜌]_ _[,]_ [ F] _𝐸𝑛𝑔𝑎𝑔𝑒𝑚𝑒𝑛𝑡_ _[ 𝑓]_ [b] _[,𝑓]_ [e] _[,]_ [ and][ F] _𝑀𝑒𝑡𝑟𝑖𝑐𝑠_ _[ 𝑓]_ [a] _[,𝑓]_ [r] [that are] _[ useful]_ [(as defined by]

Definitions 5.1 to 5.4, with the additional restriction that any nontrivial implementation of F _𝑀𝑒𝑡𝑟𝑖𝑐𝑠_ _[𝑓]_ [a] _[,𝑓]_ [r] [must] [use] [differential] [privacy)]

for a given ad campaign will not satisfy attribute privacy for some

attribute of that campaign’s audience. [8]


We prove this theorem by showing that anything that could be

learned by an advertiser in a non-private advertising system could

similarly be learned by advertiser in a private advertising system.

Proving this statement formally requires defining a game-based

privacy definition _on top_ of our UC modeling. To that end, we define

the following random variable.


**Definition 5.5.** Let EXEC _𝐸𝑛𝑣_ _[𝑓]_ [t] _[,𝜌,𝑓]_ [b] _[,𝑓]_ [e] _[,𝑓]_ [a] _[,𝑓]_ [r] _[,𝑛,]_ [D] be a random variable de
noting the output distribution of an environment _𝐸𝑛𝑣_ when interacting with the ideal functionalities F _𝑇𝑎𝑟𝑔𝑒𝑡𝑖𝑛𝑔_ _[𝑓]_ [t] _[,𝜌]_ [,][ F] _[𝑈𝑠𝑒𝑟𝐷𝑎𝑡𝑎]_ [,][ F] _𝐸𝑛𝑔𝑎𝑔𝑒𝑚𝑒𝑛𝑡_ _[ 𝑓]_ [b] _[,𝑓]_ [e] [,]

F _𝑀𝑒𝑡𝑟𝑖𝑐𝑠_ _[𝑓]_ [a] _[,𝑓]_ [r] [and] [F] _𝑆𝑜𝑐𝑖𝑒𝑡𝑦_ _[ 𝑛,]_ [D] [(connected] [as] [shown] [in] [Figure] [2)] [in] [an] [in-]

stance of the UC experiment.


Typically in a game-based definition, we have a _challenger_ that

sets up the parameters of the game (sampling randomness as needed)

and an _adversary_ that is required to guess some function of the
_challenger_ ’s randomness.


**Definition 5.6** (Distinguishing) **.** We say that an adversary A =
(A0 _, 𝐸𝑛𝑣,_ A1) succeeds in distinguishing with probability _𝑝_ with respect to a distribution D0, processing function _𝑓𝑠_ D _[ℎ]_ →{0 _,_ 1}, and
an advertising system defined by the parameters ( _𝑓_ t _, 𝜌, 𝑓_ b _, 𝑓_ e _, 𝑓_ a _, 𝑓_ r _,𝑛_ )

if:


_𝑝_ = 2 · Pr[A1 ( _𝑓𝑠_ (EXEC _𝐸𝑛𝑣_ _[𝑓]_ [t] _[,𝜌,𝑓]_ (D [b] _[,𝑓]_ 1 [e] _,𝑎𝑢𝑥_ _[,𝑓]_ [a] _[,𝑓]_ [r] ) _[,𝑛,]_ [D] _[𝑏]_ ) _,𝑎𝑢𝑥_ ) = _𝑏_ ] − 1 _,_


7In practice, this definition requires that a statistical test applied to the output of the

report generation function will still provide the same result as the test on the raw data,

albeit with an error rate of _𝛼_ . This can also be thought of as requiring the same result

of a t-test with a worse p-value.
8This is not to say that the theorem cannot hold for other instantiations of F _𝑀𝑒𝑡𝑟𝑖𝑐𝑠_ _[𝑓]_ a _[,𝑓]_ r [,]

however, all existing private metrics proposals make use of differential privacy so we

make our proof in this setting as well. We additionally require _𝜖_ _<_ 1.



$
where (D1 _,𝑎𝑢𝑥_ ) ←A0 (1 _[𝜆]_ _,_ D0) and _𝑏_ ←−{0 _,_ 1}, and the probability
is taken over the random choices of A, _𝑏_, and the execution. We
define the adversary’s advantage Adv _[𝑓]_ A [t] _[,𝜌,𝑓]_ _,_ D0 [b] _,𝑓_ _[,𝑓]_ _𝑠_ [e] _[,𝑓]_ [a] _[,𝑓]_ [r] _[,𝑛]_ := _𝑝_ .


In this definition, D0 should be thought of as the _ground truth_
distribution of features across people in society, whereas D1 represents the advertiser’s _prior knowledge_ about how people’s features

are distributed. Hence, the distance between these distributions cor
responds to the precision of information gained by distinguishing.

When D1 is close to D0, distinguishing will be more challenging,

but the advertiser can learn finer-grained information [22].

With this notion in hand, we can now state that whenever there is

an adversary that can succeed at distinguishing within non-private

advertising systems, then there exists an adversary that can succeed

in any private version of that system that preserves utility. More
precisely, a useful F _𝑀𝑒𝑡𝑟𝑖𝑐𝑠_ _[𝑓]_ [a] _[,𝑓]_ [r] [requires that the campaign size] _[ 𝑛]_ [was]
large enough to still obtain a useful result from the output of _𝑓_ r _[𝜖]_ [.]
Here, _𝑛_ plays the role of _sample complexity_ from distribution testing,

which is the number of samples necessary to distinguish between

two distributions. Thus, our approach here is to show that with a

sufficiently-sized campaign in the private setting, an adversary can

learn the same information as in the non-private setting. Borrowing

from the conventions used in distribution testing, we focus on the

goal of distinguishing with advantage [2]

3 [in the non-private setting.]

Specifically, we prove the following two lemmas.


Lemma 2. _Let_ A = (A0 _, 𝐸𝑛𝑣,_ A1) _be an adversary. Consider any_
_two ad ecosystems:_


_–_ _A non-private ecosystem with a targeting function_ _𝑓𝑡_ _that is 𝛼𝑡_ _-_
_useful with respect to a 𝑐𝑙𝑜𝑠𝑒_ _metric and the identity function 𝐼_ _as_
_the lens, an engagement function 𝑓𝑒_ _that is 𝛼𝑒_ _-useful with respect_
_to 𝑐𝑙𝑜𝑠𝑒, an attribution function 𝑓𝑎_ _that is 𝛼𝑎-useful with respect_
_to 𝑓𝑒_ _, and that uses the identity function 𝐼_ _for reporting._

_–_ _A private ecosystem with a (possibly different) targeting function_
_𝑓𝑡_ [′] _[that]_ _[is][ 𝛼]_ _𝑡_ [′] _[-useful]_ _[with]_ _[respect]_ _[to]_ _[the]_ _[same][ 𝑐𝑙𝑜𝑠𝑒]_ _[metric]_ _[and]_
_filtering lens 𝜌_ [′] _, and with a reporting function_ _𝑓𝑟_ _[𝜖]_ _[, where][ 𝜖]_ _[<]_ [1]
_that is 𝛼𝑟_ _-utility-preserving with respect to a processing function_
_𝑓𝑠_ : D _[ℎ]_ →{0 _,_ 1} _._


_For any distribution_ D0 _over_ {0 _,_ 1} _[ℓ]_ _, for_ (D1 _,𝑎𝑢𝑥_ ) ←A0 (1 _[𝜆]_ _,_ D0) _,_
_where_ D1 _has the same support as_ D0 _and both are over the domain_
X _,_ _and_ _for_ _any_ _collection_ _of_ active-ads _:_ _if_ Adv _[𝑓]_ A [t] _[,𝐼,𝑓]_ _,_ D [b] 0 _[,𝑓]_ _,𝑓_ [e] _𝑠_ _[,𝑓]_ [a] _[,𝐼,𝑛]_ ≥ 32 _[,]_



_then_ Adv _𝑓_ t [′] _[,𝜌]_ [′] _[,𝑓]_ [b] _[,𝑓]_ [e] _[,𝑓]_ [a] _[,𝑓]_ [r] _[𝜖]_ _[,𝑛]_ [′]



15 [8] _[, where][ 𝑛]_ [′] _[<]_ [100] _𝜖_ _[𝑛]_



_then_ Adv _𝑓_ At _[,𝜌]_ _,_ D _[,𝑓]_ 0 _,𝑓_ [b] _𝑠_ _[,𝑓]_ [e] _[,𝑓]_ [a] _[,𝑓]_ [r] _[,𝑛]_ ≥ 15 [8] _[, where][ 𝑛]_ [′] _[<]_ [100] _𝜖_ _[𝑛]_ - [1] 1 [+] + _[𝛼]_ _𝛼_ _[𝑡]_ _𝑡_ [′] _[𝐾][𝐾]_ _[. Here,][ 𝐾]_ _[is a]_

_computable term that depends only on_ X _,_ active-ads _,_ D0 _,_ D1 _and_
100 _𝑛_ [1][+] _[𝛼][𝑡]_ _[𝐾]_ [100] _[𝑛]_ _[𝛼][𝑡]_



At _,_ D0 _,𝑓𝑠_ ≥ [8]



_𝜖_ _𝑛_ - [1] 1 [+] + _[𝛼]_ _𝛼_ _[𝑡]_ _𝑡_ [′] _[𝐾][𝐾]_ _[<]_ [100] _𝜖_ _[𝑛]_



_𝜖_ _[𝑛]_ - _[𝛼]_ _𝛼_ _[𝑡]_ _𝑡_ [′] _[.]_



Lemma 3. _For_ _any_ _ads_ _ecosystem_ _where_ _there_ _exists_ _an_ _adver-_
_sary with distinguishing advantage_ Adv _[𝑓]_ A [t] _[,𝜌,𝑓]_ _,_ D0 [b] _,𝑓_ _[,𝑓]_ _𝑠_ [e] _[,𝑓]_ [a] _[,𝑓]_ [r] _[,𝑛]_ _>_ 0 _, this ads_
_ecosystem will not satisfy attribute privacy._


**Proof Sketch.** We formally prove Theorem 1 in Section C. It follows

immediately from Theorems 2 and 3.

To prove Theorem 2, we show that utility implies the ability to

distinguish the underlying distribution used by the ads ecosystem.

Namely, any successful distinguisher for a given advertising cam
paign in a non-private ads ecosystem can be used to construct a

distinguisher for the same campaign run in a useful, private ads



459


Proceedings on Privacy Enhancing Technologies 2026(1) Hogan et al.


ecosystem, albeit with a larger campaign size. The main idea behind

our proof of Theorem 2 is that, while the private version of an ads

ecosystem may have less utility than a non-private version, as long

as it preserves _some_ utility, then it is possible to amplify this signal

to match the utility of the non-private version. The “cost” of ampli
fication is in increasing the size of the campaign _𝑛_, which provides

the adversary with more samples to use in its distinguishing. We

can leverage distribution testing techniques [9, 18, 23] to find a
bound on this new campaign size _𝑛_ [′] . [9]


The proof of Theorem 3 follows from the definition of attribute

privacy (Definition 3.1) and the ability of our distinguisher to iden
tify the underlying distribution used by the private ads ecosystem.

Specifically, if attribute privacy were achieved for all parameters
governing the distribution of users in F _𝑆𝑜𝑐𝑖𝑒𝑡𝑦_ _[𝑛,]_ [D] [, then by definition]



the summary statistic output by F _𝑀𝑒𝑡𝑟𝑖𝑐𝑠_ _[𝑓]_ [a] _[,𝑓]_ [r] [should be independent]

of changes to this distribution. However, were this the case, then
the output of F _𝑀𝑒𝑡𝑟𝑖𝑐𝑠_ _[𝑓]_ [a] _[,𝑓]_ [r] [would be independent of the choice of] _[ 𝐷]_ [0] [or]

_𝐷_ 1 and no successful distinguisher could exist. Since Theorem 3
assumes the opposite, there must exist _some_ parameter of the underlying distribution for which attribute privacy is _not_ preserved.

We explore the idea that not every attribute may require such pro
tection, i.e., that some inferences may be acceptable, in Section 6.

## **5.3 Empirical Sample Complexity**


To provide some intuition and empirical data for the concrete sam
ple complexity increase that we showed theoretically in Theorem 2,

we implemented our ideal advertising functionalities in Python [10]


and ran the distinguishing game from Section 5.2 for concrete real
izations of our parameterizing functions and _𝛼_ -utility parameters.

Recall that _𝛼_ -utility for targeting is an indication of how tightly

the parameterizing function respects its definition of _𝑐𝑙𝑜𝑠𝑒_ (), or
how _accurate_ targeting is able to be. The expectation is that private

advertising ecosystems will likely have less of an ability to find the

true closest ad for a user—whether through using less user data or

less precise data—and we handle this challenge by decreasing the

_𝛼_ parameter for targeting from the non-private version. Relatedly,

_𝛼_ -utility for engagement represents how likely a user is to click on

an ad at all; this is entirely about user behavior, so it does not vary

between the private and non-private ad ecosystems. Our metrics

parameterizing function is instantiated using differential privacy,

as this is the most common method currently being proposed.

To run our empirical distinguishing game, we fix a campaign

with two ads ( _𝑎𝑑𝐴_ and _𝑎𝑑𝐵_ ) differing in a single bit _𝑏𝑡𝑒𝑠𝑡_, and a
distribution D0 representing the “ground truth” distribution of
users. We then create an alternate distribution D1 based off the
same covariance matrix as D0, varying the marginal probability
of _𝑏𝑡𝑒𝑠𝑡_ in D1 to gradually increase the total variation distance
between D0 and D1. We plot the sample complexity required to
distinguish D1 from D0 at a p-value of 0.05 using the uniformly

most powerful tests from Awan and Slavkovic [7] for a private

ads ecosystem in Figure 8. Then, using a standard binomial test to

distinguish, we plot a non-private version of the same ecosystem as

well as a baseline to demonstrate the increase in sample complexity.


9In practice, this demonstrates the disparate impact of differential privacy between

large advertisers with huge campaigns versus smaller independent advertisers.

[10https://github.com/kylehogan/idealAdsFunctionalities](https://github.com/kylehogan/idealAdsFunctionalities)



**Figure 8: Impact of privatization on sample complexity.**


The ‘non-private’ line still shows a substantial increase over the

baseline due to the loss of accuracy from targeting and the drop in

user engagement, while the ‘private’ line indicates the impact of

further reducing targeting accuracy and, more importantly, intro
ducing noise from differential privacy. The private and non-private

lines begin to converge at higher sample complexity due to the

lower relative impact of differential privacy for larger sample set

sizes. We provide plots on the individual impacts of _𝛼_ -targeting,

_𝛼_ -engagement, and _𝜖_ -differential privacy in Section D.

## **6 Redefining Privacy for Advertising**


Our results are a clear indication that the path forward for private

advertising requires a careful re-imagining of what privacy _should_

mean. That is, if we accept that some leakage is a necessary part of

the advertising ecosystem, then how should advertising systems rea
son about the risks posed by this leakage? To start this process, we

begin by introducing the perspectives of three stakeholder groups

that inform this future: the people receiving targeted advertise
ments, advertising networks, and regulatory bodies. By identifying

commonalities across these viewpoints—and gaps between existing

policies and end-users needs—we hope a path forward can emerge.

In Section 6.1, we employ the contextual integrity framework

[71] to help delimit when leakage may be (in)appropriate. We con
trast under what conditions users, ad tech, and regulatory bodies

consider different categories of data to be sensitive [11], and thus

deserving of stronger protections. We then outline how existing en
forcement mechanisms tailored to data sensitivity fail, despite laws

and well-aligned ad tech policies. The principles underlying exist
ing private metrics proposals offer technical solutions to some of

these gaps, but they ultimately fall short of meeting user’s privacy

expectations. We argue that future proposals for privacy-preserving

metrics must be _targeting-aware_ in order to (1) discern between the

implications of different information leakages and (2) understand

that risks associated with leakage are context dependent. In doing

so, private metrics systems of the future can apply alternate notions

of privacy, such as attribute privacy [105], that can protect sensitive

features about advertising audiences as a whole.


11In an advertising context specifically, as opposed to _generally_ sensitive.



460


Making Sense of Private Advertising Proceedings on Privacy Enhancing Technologies 2026(1)









|Appropriateness of Targeting on Feature|Col2|Col3|Col4|
|---|---|---|---|
|Sensitive Data|As perceived by...<br>People<br>Ad Tech<br>Law<br>[27, 49, 61, 80, 98]<br>[44]<br>EU[33],US[31]|As perceived by...<br>People<br>Ad Tech<br>Law<br>[27, 49, 61, 80, 98]<br>[44]<br>EU[33],US[31]|As perceived by...<br>People<br>Ad Tech<br>Law<br>[27, 49, 61, 80, 98]<br>[44]<br>EU[33],US[31]|
|health|;|;|;|
|relationship|.|.|✓|
|political beliefs|;|;|;|
|sexuality|;|;|;|
|gender|✓|8|8|
|location|.|8|8|
|age|✓|8|8|


**Figure 9:** ✓ **is used to indicate that targeting on this feature**
**is acceptable/permitted,** - **indicates that targeting is** _**always**_
**considered unacceptable/prohibited,** - **indicates that target-**
**ing is permitted unless illegal (in the case of tech policy) or**
**discriminatory (in the case of regulations), and** - **indicates**
**that targeting is** _**conditionally**_ **acceptable.**

## **6.1 Sensitivity in Advertising**


Users, advertising technology companies, and regulators all agree

that some types of information about individuals are _sensitive_ and

inappropriate to use in an advertising context. In this section, we

use the framework of contextual integrity [71] to interrogate the

conditions under which transmitting metrics from the ad network

to the advertiser are appropriate. Explicitly, contextual integrity

considers the flow of specific information types about a subject

between a sender and recipient via a transmission principle and de
termines whether this flow is _appropriate_ . In the setting of this paper,

ad networks (source) transmit various types of data (ad targeting

features) about the users (subject) in ad audiences to advertisers

(recipient) via metrics reports (transmission principle) about ad

delivery and engagement. Whether this process is perceived as

appropriate largely depends on what type of information was used

in targeting, which is the focus of Figure 9.

Contextual integrity allows us to bring nuance to our data cat
egorizations. It isn’t that advertisements can _never_ feature health

or sexuality information—in fact, it is often actively beneficial to

promote awareness of mental health support options or to advertise

events at LGBT organizations. Thus, our focus is specifically on

data sensitivity in the context using these data types (implicitly or

explicitly) to _target advertisements_ and _conduct market research_, not
simply displaying ads. [12]


The strongest ad tech policies [44] and modern, advertising
specific regulations like the Digital Services Act (DSA) [33] both

align quite closely with user preferences. Unfortunately, it has

proved difficult to enforceably put these policies into practice.


**Existing policies are sophisticated and nuanced.** While some
types of personal data (like health data) are _always_ considered sen
sitive in the context of ad targeting [33, 44, 49], other features can


12We again note that contextual advertising is a type of targeting and it is potentially

still inappropriate to conduct market research over ads that were, for example, shown

only on LGBT-focused webpages.



be more subtle. For example, while relationship status is used to

target both ads for dating services and those for divorce lawyers,

people are far less comfortable with the latter than the former, de
spite the same data being used in both cases [80]. This sentiment

is captured by ad tech policy, which prohibits targeting based on

“personal hardship”—such as divorce—or advertisements that “im
pose negativity” (e.g., body shaming). Similarly, targeting on the

basis of features that people generally find acceptable, like age or

gender, is illegal when the impact of that advertising results in

_discriminatory_ systems. As a result, regulations such as the Fair

Housing Act (FHA) [31] have been used to prohibit use of charac
teristics like age, race, and gender for all housing or employment

advertisements in the United States [72] and to enact changes to

the targeting algorithm in the same vein [91].


**Enforcement problems stymie policies’ promise.** Advertise
ment targeting is extremely opaque and largely built on machine

learning models, making both technological and legal enforcement

of these policies challenging [2, 19, 76]. The behavior of machine

learning models is prohibitively difficult to interrogate, making it

challenging to prove discrimination [3, 35, 54, 75, 88, 97]. Despite

some efforts on the part of ad networks to mitigate bias [91], it

has been found that these targeting systems distribute advertise
ments in a discriminatory way _even when it is not the intention of_
_advertisers_ [3]. It is also possible to intentionally circumvent pro
tections using proxy features or “lookalike audiences” [4, 45, 98].

Circumventing protections this way is, of course, against policy,

but even the ad networks themselves have been caught using an

opaquely-defined audience to illegally target ads to children [68].

Often the opaque nature of targeting allows companies to avoid

accountability with initial lawsuits struggling to prove discrimina
tion [40, 93]. Moreover, it took until late 2023 for courts to recognize

that ad networks, not only advertisers, are liable for the discrimi
natory targeting of ads [20]. Successful litigation has often had to

circumvent the root problem of the _use_ of sensitive data in targeted
advertising to instead focus on how that data was _collected_, relying

on regulations for deceptive business practices [30] or even wiretap
ping [10, 101]. This makes it burdensome for users to enforce their

rights. Finally, while advertising is global, regulations decidedly

are not and this ultimately limits the ability of even the strongest

regulations to protect the privacy of all users.

## **6.2 Metrics is Sensitivity Agnostic**


If there existed meaningful enforcement of existing laws and ad

targeting policies—and confidence that these strong laws and poli
cies applied across the full advertising ecosystem—then perhaps we

would not need to be as concerned about information leakage. But

without such enforcement, there is a real risk that the information

leaking from the system will directly concern sensitive data. In this

section, we turn our attention to metrics in the hope that it can

make up for the identified failures of targeting.


**Metrics is well-positioned to facilitate policy enforcement.**

Metrics does not face the same structural challenges that make

aligning targeting systems and people’s privacy preferences so

difficult. First, the fraught (and legally tricky) decisions on which

advertisements should be shown to which users have already been

made. Second, the systems that collect and compute metrics are



461


Proceedings on Privacy Enhancing Technologies 2026(1) Hogan et al.



dramatically simpler and more transparent than those used to tar
get advertisements. Thus, the metrics infrastructure could aid in

identifying and documenting policy violations.

Users also have significant agency that they can exert when it

comes to metrics. While users have no choice in the advertising

networks to which they are subjected while browsing the internet,

users’ choice of browsers and devices are directly tied to the way

their data is collected and processed within metrics. In principle,

this creates an opportunity for organizations to compete in order

to make their metrics systems as well-aligned with user’s privacy

preferences as possible. Indeed, different groups of ad tech compa
nies are currently working on competing proposals for privatizing

metrics [42, 47]. Importantly, these proposals are designed to be

_interoperable_, meaning that no matter which system was used to

target an advertisement, a variety of organizations, each offering

a different suite of privacy protections, are capable of producing

equivalent metrics output. [13]


**Current** **proposals** **fall** **short.** We identify three reasons why

current proposals for privacy-preserving metrics do not adequately

enforce policy. First, their technical underpinnings rely on aggre
gation [32] and the injection of statistically-calibrated noise (i.e.,

differential privacy [37]). The result is an implicit understanding

that privacy in advertising is about preserving the _confidentiality_

of individuals’ features. As we observe in this work, however, some

amount of leakage is inherent, and the leakage these systems per
mit is fundamentally de-contextualized, i.e., it is at odds with the

understanding that _not all types of data should be treated the same_,

as demonstrated by Figure 9.

Second, current metrics proposals are unaware of the content

and target audience of the advertisements whose performance they

measure. Thus, an ad campaign promoting clothing is treated identi
cally to an ad campaign promoting therapy, despite the difference in

the sensitivity of the data likely used to target these advertisements.

Similarly, an ad campaign that uses gender to target clothing adver
tisements is indistinguishable from one that uses gender to target

employment advertisements, despite the difference in how the law

sees these campaigns. This is intentional; existing metrics systems

embraced data minimization within their design, and differentiat
ing between advertisements or audience would require collating

this data across multiple systems (i.e., from targeting systems to

metrics systems). While data minimization is generally the right

approach for system design, in this case it has rendered metrics

incapable of discerning between information leakages that people

might consider harmful and innocuous.

Third, existing metrics proposals treat differential privacy as a

privacy panacea, when, in fact, there are cases in which inference

itself can be harmful (see Section 3.2). Specifically, there are audi
ence types, so called “custom audiences,” defined using personally

identifiable information. In these cases, the inference facilitated

by differential privacy has qualitatively different risk as learning a

feature of their audience also gives them confidence that _individual_
_members_ of the audience possess this feature, contrary to the likely

expectations of those audience members.


13User choice alone is likely insufficient to ensure that people’s privacy is protected

according to their preferences—default settings and other dark patterns are often

successful in preventing users from exercising their ability to choose effectively [12].


## **6.3 Closing the Gap**

While data sensitivity provides clear intuition for managing the

risks of information leakage, the existing paradigms within which

advertising systems are designed are insufficient to actualize this

approach. Within targeting, there has been significant policy work

to set standards for the treatment of sensitive data, but there are

structural barriers to enforcing these policies. On the other hand,

emerging metrics proposals are technologically sophisticated and

relatively transparent, but are _incapable_ of enforcing normative pri
vacy policies because they lack context on how ads were targeted.

In order to close this gap, we advocate for expanding the ap
proaches that are being used to think about privacy when devel
oping new targeting and metrics proposals. There is tremendous,

ongoing technical work integrating differential privacy into met
rics computation in which researchers are leveraging cutting-edge

privacy-enhancing technologies to significantly improve people’s

concrete privacy [47, 90]. These efforts, however, cannot be the sum

total of the solution. Specifically, future developments need to ap
ply the same, policy-oriented analyses to metrics that are currently

being applied to targeting. We advocate for the inclusion of group

privacy notions, like attribute privacy, that explicitly account for

privacy harms not covered by differential privacy. Namely, as we

introduced in Section 3.2 and expanded on here, _what_ data is leaked
can be just as, if not more, important than _how much_ information

is revealed. This is especially true because advertisers can combine

“private” metrics with already-known information about individual

members of the audience, such as their identities [81].

However, applying attribute privacy to advertising metrics re
quires co-design across targeting and metrics protocols as, while

targeting possesses the necessary information about data sensitivity,

metrics does not. Distributional privacy notions naturally require

information about the underlying distribution of users targeted

by an advertisement which is not currently available to metrics

protocols. Making metrics systems _targeting-aware_ by giving it

the audience information for its reports would allow for the use

of definitions like attribute privacy and could, perhaps, even per
mit metrics protocols to monitor targeting for policy violations or

discriminatory behavior. When targeting and metrics are run by

different organizations, there may even be incentives to do this type

of mutual monitoring. While there will no-doubt be significant tech
nical (and even legal) hurdles in implementing such a vision, the

result would be better alignment between the privacy preferences

of users and the privacy properties of advertising ecosystems.

## **7 Conclusion**


In this work we have taken a step back to study what notions of

privacy are possible within advertising. We showed that any adver
tising system that is even minimally useful must also allow some

amount of information leakage. Taking this as a given, we identify

the sensitivity of data as an important consideration when it comes

to managing this leakage—a decision which has significant impli
cations on how future privacy-preserving advertising proposals

should be designed.



462


Making Sense of Private Advertising Proceedings on Privacy Enhancing Technologies 2026(1)


## **Acknowledgments**

This research was supported by the DARPA SIEVE program un
der Agreement No. HR00112020021 and by the National Science

Foundation under Grants No. 1955270, 2209194, 2217770, 2228610,

2230670, and 2330065.

## **References**


[1] Claire L Adida, Adeline Lo, Lauren Prather, and Scott Williamson. 2022. Refugees

to the rescue? Motivating pro-refugee public engagement during the COVID-19
pandemic. _Journal of Experimental Political Science_ 9, 3 (2022), 281–295.

[2] John Albert. 2023. Not a solution: Meta’s new AI system to contain discrimina
tory ads. [https://algorithmwatch.org/en/meta-discriminatory-ads/.](https://algorithmwatch.org/en/meta-discriminatory-ads/) Accessed

January 2025.

[3] Muhammad Ali, Angelica Goetzen, Alan Mislove, Elissa M. Redmiles, and Piotr

Sapiezynski. 2023. Problematic Advertising and its Disparate Exposure on
Facebook. In _32nd USENIX Security Symposium (USENIX Security 23)_ . USENIX

Association, Anaheim, CA, 5665–5682. [https://www.usenix.org/conference/](https://www.usenix.org/conference/usenixsecurity23/presentation/ali)

[usenixsecurity23/presentation/ali](https://www.usenix.org/conference/usenixsecurity23/presentation/ali)

[4] Athanasios Andreou, Márcio Silva, Fabrício Benevenuto, Oana Goga, Patrick

Loiseau, and Alan Mislove. 2019. Measuring the Facebook advertising ecosystem. In _NDSS 2019-Proceedings of the Network and Distributed System Security_
_Symposium_ . 1–15.

[5] Apple. 2023. Private Ad Measurement. [https://github.com/patcg-individual-](https://github.com/patcg-individual-drafts/private-ad-measurement)

[drafts/private-ad-measurement.](https://github.com/patcg-individual-drafts/private-ad-measurement) Accessed January 2025.

[6] Adrián Astorgano. 2023. From “Heavy Purchasers” of Pregnancy Tests to

the Depression-Prone: We Found 650,000 Ways Advertisers Label You. [https:](https://themarkup.org/privacy/2023/06/08/from-heavy-purchasers-of-pregnancy-tests-to-the-depression-prone-we-found-650000-ways-advertisers-label-you)

[//themarkup.org/privacy/2023/06/08/from-heavy-purchasers-of-pregnancy-](https://themarkup.org/privacy/2023/06/08/from-heavy-purchasers-of-pregnancy-tests-to-the-depression-prone-we-found-650000-ways-advertisers-label-you)

[tests-to-the-depression-prone-we-found-650000-ways-advertisers-label-you.](https://themarkup.org/privacy/2023/06/08/from-heavy-purchasers-of-pregnancy-tests-to-the-depression-prone-we-found-650000-ways-advertisers-label-you)
_The Markup_ (2023). Accessed January 2025.

[7] Jordan Alexander Awan and Aleksandra Slavkovic. 2020. Differentially Private
Inference for Binomial Data. _Journal of Privacy and Confidentiality_ 10, 1 (Jan.

2020). [https://doi.org/10.29012/jpc.725](https://doi.org/10.29012/jpc.725)

[8] Michael Backes, Aniket Kate, Matteo Maffei, and Kim Pecina. 2012. Obliviad:
Provably secure and practical online behavioral advertising. In _2012 IEEE Sym-_
_posium on Security and Privacy_ . IEEE, 257–271.

[9] Ziv Bar-Yossef. 2002. _The complexity of massive data set computations_ . University

of California, Berkeley.

[10] Kat Black. 2024. LinkedIn sued for tracking user health data. [https:](https://www.benefitspro.com/2024/10/29/linkedin-hit-with-wave-of-health-data-claims-under-california-privacy-law-412-177165/?slreturn=20250120132542)

[//www.benefitspro.com/2024/10/29/linkedin-hit-with-wave-of-health-data-](https://www.benefitspro.com/2024/10/29/linkedin-hit-with-wave-of-health-data-claims-under-california-privacy-law-412-177165/?slreturn=20250120132542)

[claims-under-california-privacy-law-412-177165/?slreturn=20250120132542.](https://www.benefitspro.com/2024/10/29/linkedin-hit-with-wave-of-health-data-claims-under-california-privacy-law-412-177165/?slreturn=20250120132542)

Accessed on 20 January 2025..

[11] Benjamin E. Borenstein and Charles R. Taylor. 2024. The effects of targeted digital advertising on consumer welfare. _Journal_ _of_ _Strategic_ _Market-_
_ing_ 32, 3 (2024), 317–332. [https://doi.org/10.1080/0965254X.2023.2218865](https://doi.org/10.1080/0965254X.2023.2218865)

[arXiv:https://doi.org/10.1080/0965254X.2023.2218865](https://arxiv.org/abs/https://doi.org/10.1080/0965254X.2023.2218865)

[12] Christoph Bösch, Benjamin Erb, Frank Kargl, Henning Kopp, and Stefan Pfatthe
icher. 2016. Tales from the dark side: Privacy dark strategies and privacy dark
patterns. _Proceedings on Privacy Enhancing Technologies_ (2016).

[13] Sanaz Taheri Boshrooyeh, Alptekin Küpçü, and Öznur Özkasap. 2018. PPAD:
Privacy preserving group-based advertising in online social networks. In _2018_
_IFIP Networking Conference (IFIP Networking) and Workshops_ . IEEE, 1–9.

[14] Michael Braun, Bart de Langhe, Stefano Puntoni, and Eric M Schwartz.

2024. Leveraging Digital Advertising Platforms for Consumer Research. _Journal_ _of_ _Consumer_ _Research_ 51, 1 (05 2024), 119–128.

[https://doi.org/10.1093/jcr/ucad058 arXiv:https://academic.oup.com/jcr/article-](https://doi.org/10.1093/jcr/ucad058)

[pdf/51/1/119/57655198/ucad058.pdf](https://arxiv.org/abs/https://academic.oup.com/jcr/article-pdf/51/1/119/57655198/ucad058.pdf)

[15] Steven Brill. 2024. You Think You Know How Misinformation Spreads? Welcome

to the Hellhole of Programmatic Advertising. [https://www.wired.com/story/](https://www.wired.com/story/death-of-truth-misinformation-advertising/)

[death-of-truth-misinformation-advertising/.](https://www.wired.com/story/death-of-truth-misinformation-advertising/) Accessed January 2025.

[16] Moritz Büchi, Noemi Festic, and Michael Latzer. 2022. The chilling effects of
digital dataveillance: A theoretical model and an empirical research agenda. _Big_
_Data & Society_ 9, 1 (2022), 20539517211065368.

[17] José González Cabañas, Ángel Cuevas, and Rubén Cuevas. 2018. Unveiling and

Quantifying Facebook Exploitation of Sensitive Personal Data for Advertising
Purposes. In _USENIX Security 2018_, William Enck and Adrienne Porter Felt (Eds.).

USENIX Association, 479–495.

[18] Bryan Cai, Constantinos Daskalakis, and Gautam Kamath. 2017. Priv’it: Private
and sample efficient identity testing. In _International Conference on Machine_
_Learning_ . PMLR, 635–644.

[19] Giuseppe Calderonio, Mir Masood Ali, and Jason Polakis. 2024. Fledging Will
Continue Until Privacy Improves: Empirical Analysis of Google’s {PrivacyPreserving} Targeted Advertising. In _33rd USENIX Security Symposium (USENIX_
_Security 24)_ . 4121–4138.

[20] Third Division California Court of Appeals, First District. 2023. Liapes v. Face
book, Inc. [https://casetext.com/case/liapes-v-facebook-inc.](https://casetext.com/case/liapes-v-facebook-inc)




[21] Ran Canetti. 2001. Universally Composable Security: A New Paradigm for
Cryptographic Protocols. In _42nd FOCS_ . IEEE Computer Society Press, 136–145.

[https://doi.org/10.1109/SFCS.2001.959888](https://doi.org/10.1109/SFCS.2001.959888)

[22] Clément L. Canonne. 2020. _A Survey on Distribution Testing: Your Data is Big._
_But is it Blue?_ Number 9 in Graduate Surveys. Theory of Computing Library.

1–100 pages. [https://doi.org/10.4086/toc.gs.2020.009](https://doi.org/10.4086/toc.gs.2020.009)

[23] Clément L Canonne, Gautam Kamath, Audra McMillan, Adam Smith, and

Jonathan Ullman. 2019. The structure of optimal private tests for simple hypotheses. In _Proceedings of the 51st Annual ACM SIGACT Symposium on Theory_
_of Computing_ . 310–321.

[24] Juan Miguel Carrascosa, Jakub Mikians, Ruben Cuevas, Vijay Erramilli, and

Nikolaos Laoutaris. 2015. I always feel like somebody’s watching me: measuring
online behavioural advertising. In _Proceedings of the 11th ACM Conference on_
_Emerging Networking Experiments and Technologies_ . 1–13.

[25] Claude Castelluccia, Mohamed-Ali Kaafar, and Minh-Dung Tran. 2012. Betrayed
by your ads! reconstructing user profiles from targeted ads. In _Proceedings of the_
_12th International Conference on Privacy Enhancing Technologies_ (Vigo, Spain)
_(PETS’12)_ . Springer-Verlag, Berlin, Heidelberg, 1–17. [https://doi.org/10.1007/](https://doi.org/10.1007/978-3-642-31680-7_1)

[978-3-642-31680-7_1](https://doi.org/10.1007/978-3-642-31680-7_1)

[26] Eugene Y Chan and Jasmina Ilicic. 2019. Political ideology and brand attachment.
_International Journal of Research in Marketing_ 36, 4 (2019), 630–646.

[27] Farah Chanchary and Sonia Chiasson. 2015. User Perceptions of Sharing, Advertising, and Tracking. In _Eleventh Symposium On Usable Privacy and Security_
_(SOUPS 2015)_ . USENIX Association, Ottawa, 53–67. [https://www.usenix.org/](https://www.usenix.org/conference/soups2015/proceedings/presentation/chanchary)

[conference/soups2015/proceedings/presentation/chanchary](https://www.usenix.org/conference/soups2015/proceedings/presentation/chanchary)

[28] Salim Chouaki, Islem Bouzenia, Oana Goga, and Beatrice Roussillon. 2022.

Exploring the online micro-targeting practices of small, medium, and large
businesses. _Proceedings of the ACM on Human-Computer Interaction_ 6, CSCW2

(2022), 1–23.

[29] Wolfie Christl, Katharina Kopp, and Patrick Urs Riechert. 2017. Corporate
surveillance in everyday life. _Cracked Labs_ 6 (2017), 2017–10.

[30] US Federal Trade Comission. 2022. FTC Charges Twitter with

Deceptively Using Account Security Data to Sell Targeted Ads .

[https://www.ftc.gov/news-events/news/press-releases/2022/05/ftc-charges-](https://www.ftc.gov/news-events/news/press-releases/2022/05/ftc-charges-twitter-deceptively-using-account-security-data-sell-targeted-ads)

[twitter-deceptively-using-account-security-data-sell-targeted-ads.](https://www.ftc.gov/news-events/news/press-releases/2022/05/ftc-charges-twitter-deceptively-using-account-security-data-sell-targeted-ads) Accessed

on 20 January 2025..

[31] U.S. Congress. 1970. United States Code: Fair Housing, 42 U.S.C. §§3601 - 3619.

[32] Henry Corrigan-Gibbs and Dan Boneh. 2017. Prio: Private, Robust, and Scalable
Computation of Aggregate Statistics. In _14th USENIX Symposium on Networked_
_Systems_ _Design_ _and_ _Implementation_ _(NSDI_ _17)_ . USENIX Association, Boston,

MA, 259–282. [https://www.usenix.org/conference/nsdi17/technical-sessions/](https://www.usenix.org/conference/nsdi17/technical-sessions/presentation/corrigan-gibbs)

[presentation/corrigan-gibbs](https://www.usenix.org/conference/nsdi17/technical-sessions/presentation/corrigan-gibbs)

[33] Council of European Union. 2022. Regulation (EU) 2022/2065 of the Eu
ropean Parliament and of the Council (Digital Services Act). [https://eur-](https://eur-lex.europa.eu/eli/reg/2022/2065/oj/eng)

[lex.europa.eu/eli/reg/2022/2065/oj/eng.](https://eur-lex.europa.eu/eli/reg/2022/2065/oj/eng) Accessed on 20 January 2025. Also

see [https://commission.europa.eu/strategy-and-policy/priorities-2019-2024/](https://commission.europa.eu/strategy-and-policy/priorities-2019-2024/europe-fit-digital-age/digital-services-act_en)

[europe-fit-digital-age/digital-services-act_en..](https://commission.europa.eu/strategy-and-policy/priorities-2019-2024/europe-fit-digital-age/digital-services-act_en)

[34] Matthew Crain and Anthony Nadler. 2019. Political manipulation and internet
advertising infrastructure. _Journal of Information Policy_ 9 (2019), 370–410.

[35] Amit Datta, Anupam Datta, Jael Makagon, Deirdre K Mulligan, and Michael Carl

Tschantz. 2018. Discrimination in Online Advertising: A Multidisciplinary
Inquiry. _Conference_ _on_ _Fairness,_ _Accountability,_ _and_ _Transparency_ 81 (2018),

20–34.

[36] Soteris Demetriou, Whitney Merrill, Wei Yang, Aston Zhang, and Carl A. Gunter.

2016. Free for All! Assessing User Data Exposure to Advertising Libraries on
Android. In _NDSS 2016_ . The Internet Society.

[37] Cynthia Dwork, Frank McSherry, Kobbi Nissim, and Adam Smith. 2006. Calibrating Noise to Sensitivity in Private Data Analysis. In _TCC_ _2006_ _(LNCS,_
_Vol._ _3876)_, Shai Halevi and Tal Rabin (Eds.). Springer, Heidelberg, 265–284.

[https://doi.org/10.1007/11681878_14](https://doi.org/10.1007/11681878_14)

[38] Cynthia Dwork and Deirdre K Mulligan. 2013. It’s not privacy, and it’s not fair.
_Stan. L. Rev. Online_ 66 (2013), 35.

[39] ean Halliday. 2002. Gay Ride. [https://adage.com/article/news/gay-ride/52730](https://adage.com/article/news/gay-ride/52730)

[40] United States District Court for the District of Maryland. 2021. Opiotennione v.

Bozzuto Mgmt. [https://casetext.com/case/opiotennione-v-bozzuto-mgmt-co.](https://casetext.com/case/opiotennione-v-bozzuto-mgmt-co)

Civil No. 20-1956 PJM.

[41] Avi Goldfarb and Catherine Tucker. 2011. Chapter 6 - Online Advertising.

Advances in Computers, Vol. 81. Elsevier, 289–315. [https://doi.org/10.1016/B978-](https://doi.org/10.1016/B978-0-12-385514-5.00006-9)

[0-12-385514-5.00006-9](https://doi.org/10.1016/B978-0-12-385514-5.00006-9)

[42] Google. 2021. Attribution Reporting for Web overview. [https://developers.](https://developers.google.com/privacy-sandbox/private-advertising/attribution-reporting)

[google.com/privacy-sandbox/private-advertising/attribution-reporting.](https://developers.google.com/privacy-sandbox/private-advertising/attribution-reporting) Ac
cessed: 21 January 2025.

[43] Google. 2024. Topics API. [https://developers.google.com/privacy-sandbox/](https://developers.google.com/privacy-sandbox/private-advertising/topics)

[private-advertising/topics.](https://developers.google.com/privacy-sandbox/private-advertising/topics) Accessed January 2025.

[44] Google. 2025. Google Ads policies - Advertising Policies Help. [https://support.](https://support.google.com/adspolicy/)

[google.com/adspolicy/.](https://support.google.com/adspolicy/) Accessed on 20 January 2025.

[45] Google. 2025. Use Lookalike segments to grow your audience. [https://support.](https://support.google.com/google-ads/answer/13541369?hl=en)

[google.com/google-ads/answer/13541369?hl=en.](https://support.google.com/google-ads/answer/13541369?hl=en) Accessed on 20 January 2025..



463


Proceedings on Privacy Enhancing Technologies 2026(1) Hogan et al.




[46] Matthew Green, Watson Ladd, and Ian Miers. 2016. A protocol for privately
reporting ad impressions at scale. In _Proceedings of the 2016 ACM SIGSAC Con-_
_ference on Computer and Communications Security_ . 1591–1601.

[47] W3C Private Advertising Technology Community Group. 2024. Privacy
Preserving Attribution: Level 1. [https://patcg.github.io/ppa-api/.](https://patcg.github.io/ppa-api/) Accessed

January 2025.

[48] Saikat Guha, Bin Cheng, and Paul Francis. 2011. Privad: Practical privacy
in online advertising. In _USENIX conference on Networked systems design and_
_implementation_ . 169–182.

[49] Julia Hanson, Miranda Wei, Sophie Veys, Matthew Kugler, Lior Strahilevitz, and

Blase Ur. 2020. Taking Data Out of Context to Hyper-Personalize Ads: Crowd
workers’ Privacy Perceptions and Decisions to Disclose Private Information. In
_Proceedings of the 2020 CHI Conference on Human Factors in Computing Systems_
(Honolulu, HI, USA) _(CHI ’20)_ . Association for Computing Machinery, New York,

NY, USA, 1–13. [https://doi.org/10.1145/3313831.3376415](https://doi.org/10.1145/331383