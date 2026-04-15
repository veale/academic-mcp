Artificial Intelligence Review (2025) 58:223
[https://doi.org/10.1007/s10462-025-11170-5](https://doi.org/10.1007/s10462-025-11170-5)

## **Exploring privacy mechanisms and metrics in federated**

**learning**


**Dhanya Shenoy** **[1]** **· Radhakrishna Bhat** **[1]** **· Krishna Prakasha K** **[2]**


Accepted: 28 February 2025 / Published online: 3 May 2025
© The Author(s) 2025


**Abstract**
The federated learning (FL) principle ensures multiple clients jointly develop a machine
learning model without exchanging their local data. Various government enacted prohibi­
tion policies on data exchange between organizations have led to the need for privacypreserved federated learning. Many industries have cultivated this idea of model develop­
ment through federated learning to enhance performance and accuracy. This paper offers a
detailed overview of the background of FL, highlighting existing aggregation algorithms,
frameworks, implementation aspects, and dataset repositories, establishing itself as an
essential reference for researchers in the field. The paper thoroughly reviews existing
centralized and decentralized FL approaches proposed in the literature and gives an over­
view about the methodology, privacy techniques implemented and limitations to guide
other researchers to advance their research in the field of federated learning. The paper
discusses the critical role of privacy-enhancing technologies like differential privacy (DP),
homomorphic encryption (HE), and secure multiparty computation (SMPC) in federated
learning highlighting their effectiveness in safeguarding sensitive data while optimizing
the balance between privacy, communication efficiency, and computational cost. The pa­
per explores the applications of federated learning in privacy-sensitive areas like natural
language processing (NLP), healthcare, and Internet of Things (IoT) with edge computing.
We believe our work provides a novel addition by identifying privacy evaluation metrics
and spotlighting the measures in terms of data privacy and correctness, communication
cost, computational cost and scalability. Furthermore, it identifies emerging challenges and
suggests promising research directions in the federated learning domain.


**Keywords** Federated learning · Privacy preservation · Differential privacy ·
Decentralized · Blockchain


Extended author information available on the last page of the article

# `1 3`


**223** Page 2 of 51

## **1 Introduction**



D. Shenoy et al.



The advent of recent streams of artificial intelligence (AI), machine learning (ML), Internet
of Things (IoT), etc., has driven a vast amount of data accumulation called big data. These
data have been applied to streams such as transportation, industry, agriculture, healthcare,
and so on. Organizations take valuable insights from these data to better understand user
behavior, which helps them grow their customer base and boost profits. For example, banks
evaluate customers based on their loan and credit card history, companies analyze user data
collected from wearable watches to develop health-related applications, and retail com­
panies increase their business by utilizing recommendation systems that predict based on
browsing and shopping patterns. Building an AI or ML model requires integrating data from
several sources. However, obtaining this kind of data could be costly and time-consuming.
Meanwhile, specific policies prohibit data exchange between organizations due to increased
privacy and data security expectations. For instance, the US government enacted the Health
Insurance Portability and Accountability Act (HIPAA) to prevent hospitals, insurance com­
panies, and healthcare providers from accessing private and sensitive patient data. The
General Data Protection Regulation (GDPR), enacted by the European Union, prohibits
institutions and organizations from using users’ personal data without consent (Khan et al.
2023). The GDPR states that operators must have user agreements and cannot manipulate
users into sacrificing their privacy. Operators are also not allowed to train models with­
out the user’s consent (Albrecht 2016). Similarly, network providers are prohibited from
destroying or disclosing personal data by China’s General Security Law (Zhang et al. 2021).
It emphasizes that network operators are not allowed to reveal, alter, or destroy the personal
data they gather. Before proceeding with any data transactions with the third party, ensur­
ing that the proposed contract clearly defines the scope of the data to be shared and the data
protection criteria (Parasol 2018) is imperative. Countries like Argentina’s Personal Data
Protection Act (PDPA), Japan’s Act on the Protection of Personal Information (APPI), and
Canada’s Personal Information Protection and Electronic Documents Act (PIPEDA), etc. to
name a few are strict laws that protect data privacy in their respective international organi­
zations or countries (Li et al. 2023a). The implementation of these laws helps in preventing
data leaks and advancing security. These guidelines aid in safeguarding user data while
creating difficulties for the training of AI or ML models.

To address these training difficulties, Google proposed the federated learning concept,
which takes place across a federation of dispersed learners in the system using locally
stored data for model training. Federated learning minimizes the costs and privacy con­
cerns related to traditional centralized machine learning methods by following two funda­
mental principles: local computing and model transfer. Every learner in federated learning
can improve their local model without having direct access to other learners’ private data.
Federated learning (FL) is a machine learning environment where several clients exchange
training updates with each other to develop a high-performing centralized model. Here, each
client’s learning algorithm updates the local model with its local data in each round of FL.
The clients subsequently send the update to server, which combines the client updates to
produce a global model.

FL has emerged as a crucial framework in the age of data-driven innovation, address­
ing the growing need for privacy-focused machine learning. Unlike traditional methods
that rely on centralized data aggregation for training models, FL eliminates the associated

# `1 3`


Exploring privacy mechanisms and metrics in federated learning



Page 3 of 51 **223**



privacy risks, regulatory hurdles, and security challenges-especially relevant in sensitive
fields like healthcare, finance, and IoT. Instead, FL enables models to be trained directly
on decentralized data stored across multiple devices or institutions, eliminating the neces­
sity to transfer sensitive data to a central server. This decentralized approach enhances data
privacy, ensures compliance with regulations such as GDPR, and reduces communication
overhead by utilizing edge computing. Additionally, FL fosters collaborative model training
across diverse datasets, improving both generalization and fairness while maintaining data
sovereignty. As data volumes and sensitivity continue to grow, FL offers an effective and
scalable solution to balance the demands of privacy and performance in modern AI systems.

FL is designed to protect data privacy by training models on distributed data without
centralized aggregation. However, the inherent structure of FL still exposes vulnerabili­
ties, such as information leakage through model updates, membership inference attacks,
and gradient inversion techniques. This necessitates the development and evaluation of
robust privacy-preserving mechanisms and assessing the effectiveness of these mechanisms
requires well-defined metrics to quantify privacy guarantees without compromising model
performance. The motivation behind this survey paper is to consolidate existing privacy
techniques and metrics, offering a comprehensive resource for researchers and practitioners.
It helps identify and highlights challenges in balancing privacy, efficiency, and accuracy,
and sets the stage for future research in securing FL systems. Providing information for the
growing adoption of FL in sensitive domains like healthcare, natural language processing
(NLP), and IoT, understanding privacy trade-offs and advancements is crucial for ensuring
safe and trustworthy deployment of federated systems.

### **1.1 Positioning our work among existing FL research**


Several surveys have been conducted to examine federated learning (FL) from various
perspectives, each emphasizing different aspects, challenges, or application domains. For
example, Zhang et al. (2021) reviewed the evolution of FL and provided an overview of
existing research based on data partitioning, privacy, machine learning models, communica­
tion architecture, and heterogeneity. It also briefly analyzed current real-world applications
of FL. Similarly, Khan et al. (2023) introduced a taxonomy for FL, highlighted key consider­
ations, identified factors crucial for ensuring privacy, and discussed the primary techniques
utilized. Additionally, they summarized the various application domains of FL. The article
(Li et al. 2023a) provides a thorough analysis of the application and security of federated
learning in healthcare. It provides insights into applications in the medical domain using
a federated aggregation approach. It analyses security of FL, presents privacy-preserving
techniques of FL. The authors (Gabrielli et al. 2023) provide detailed information on decen­
tralized FL solutions that enable FL clients to collaborate and communicate without a cen­
tral server, and also concentrated on the crucial problems of the centralized orchestration in
traditional FL client–server architecture. They categorized existing approaches into distrib­
uted computing and blockchain-based approaches. The paper (Liu et al. 2024) is yet another
survey on blockchain-enabled FL that introduced the fundamental concepts of blockchain
and FL while exploring their interrelationship. Additionally, it offers an in-depth analysis of
FL system architectures, covering the various layers like infrastructure, algorithm, network,
communication, blockchain consensus, and application and it discusses the current state of
research. Sameera et al. (2024) examined the scientific community’s research attempts to

# `1 3`


**223** Page 4 of 51



D. Shenoy et al.



define privacy solutions in implementing Blockchain-Enabled FL situations. They provided
a thorough overview of the history of FL and Blockchain, assessed current architectures for
their compatibility, and listed the main threats and potential defences to ensure privacy in
their environment. Table 1 presents a comparison of key topics covered in related survey
articles along with our contributions. This comparison highlights how our survey can assist
both academic researchers and industry professionals in understanding existing theories and
techniques aimed at enhancing FL performance in both centralized and decentralized set­
tings. Unlike previous surveys, our work not only explores privacy-enhancing techniques
but also provides an understanding of various privacy metrics for performance evaluation.
By providing a comprehensive overview of the current landscape, this survey serves as a
valuable resource for gaining insights, understanding emerging trends, and guiding future
advancements in the field of FL.

### **1.2 Contribution and structure of the article**


The contributions of this paper are multifaceted and provide significant insights into the
evolving field of FL. (1) Provides a comprehensive background on FL and associated chal­
lenges. (2) In-depth discussion on privacy-preserving techniques highlighting their trade-offs
and applicability in FL systems. (3) Categorizing existing FL architectures into centralized
and decentralized models, evaluating their strengths and limitations. (4) Explores FL appli­
cations across multiple domains, emphasizing practical implementations and potential chal­
lenges. (5) Analysis of privacy and performance trade-offs using various evaluation metrics,
focusing data privacy and correctness, resistance to attacks, computational and communica­
tion overhead and scalability of FL systems.

The structure of this paper is organized as follows. Section 2 gives the background and
preliminaries of federated learning, covering its fundamental principles, classification,
aggregation algorithms, benchmark frameworks, and key challenges. Section 3 explores the



**Table 1** Comparison of federated learning studies
Paper FL Literature review
basics



Literature review Privacy
mechanism



Literature review Privacy Applications Privacy
mechanism metrics

Centralized Decentralized NLP Health IoT
care



IoT



Zhang
et al.
(2021)

Khan
et al.
(2023)

Li et al.
(2023a)

Gabri­
elli et al.
(2023)

Liu et al.
(2024)

Sameera
et al.
(2024)



✓ ✓ ✗ ✓ ✓ ✓ ✓ ✗


✓ ✗ ✗ ✗ ✓ ✓ ✓ ✗


✓ ✗ ✓ ✓ ✗ ✓ ✗ ✗


✓ ✗ ✓ ✓ ✗ ✗ ✗ ✗


✓ ✗ ✓ ✓ ✗ ✗ ✗ ✗


✓ ✗ ✓ ✓ ✗ ✗ ✗ ✗



Our work ✓ ✓ ✓ ✓ ✓ ✓ ✓ ✓

# `1 3`


Exploring privacy mechanisms and metrics in federated learning



Page 5 of 51 **223**



significance of privacy protection in FL and examines essential technologies employed to
safeguard user data. Section 4 summarizes the existing literature review based on the archi­
tecture of FL namely centralized and decentralized FL. Section 5 discusses the applications
of FL across various domains, including natural language processing (NLP), healthcare,
IoT, and edge computing. In Sect. 6, we discuss privacy and performance analysis with vari­
ous metric considerations. Sectios 7 and 8 outlines the future scope and provide concluding
remarks.

## **2 Background and preliminaries**


In this section, we present the concepts of federated learning, its categorization in terms of
architecture, federation scale and data distribution across sampled feature space, and also
aggregation algorithms and benchmark frameworks.

### **2.1 Federated learning**


Federated learning is an environment where several clients can work together to train mod­
els using their local data and only exchange the training parameters required to achieve
the learning goal, maintaining the privacy of their original data, as illustrated in Fig. 1.
This configuration guarantees the privacy of each client’s data while enabling dispersed


**Fig. 1** A schematic diagram of federated learning

# `1 3`


**223** Page 6 of 51



D. Shenoy et al.



data training. With federated learning, multiple participants, _P_ 1 _, . . ., Pn_, are chosen to
train the machine learning model M without aggregating their individual data _D_ 1 _, . . ., Dn_
. Together, they create the model _MF ED_, which has _δF ED_ performance. In contrast, tradi­
tional machine learning with multiple participants would require aggregating all of the data
together, _D_ = _D_ 1 ∪ _D_ 2 ∪ _· · ·_ ∪ _Dn_, to train model _MSUM_, which would have a perfor­
mance of _δSUM_ . The accuracy loss in FL is represented by delta ( _δ_ ), where _δ_ is a positive
real number, and it holds that _|δF ED −_ _δSUM_ _| < δ_ (Nagar 2019). In each round of FL, _w_ 1
to _wn_ denote the weights of the _n_ clients, while _w_ _[′]_ represents the global model’s weight.

The following steps constitute the fundamentals of multi-party collaborative modeling
in FL:



1. **Client selection:** The server keeps track of potential _client_ 1 to _clientn_, which are usu­

ally edge servers, IoT devices, or smartphones. The server chooses _k_ node clients from
a collection of clients that satisfy the requirements for the FL process.
2. **Broadcasting:** A machine learning model called model _M_ is initialized on the server.

Weights and biases in the model are established via random initialization or through
pre-training with the available global dataset. This initial model _M_ is sent to every
selected client by the server.
3. **Client local training:** Among the _k_ clients chosen by the server, each _clienti_, where

1 _≤_ _i ≤_ _n_ will use its local data to train the initial model locally. It will then use the
gradient descent technique to optimize the learning model, encrypt it, and share the
updated model with the server.
4. **Aggregation:** The local model updates are gathered by the server from the _k_ clients,

validated, and the lagging updates are removed and then aggregated to create the global
model _MF ED_ .
5. **Model update:** For the current round _j_, the cumulative update _Uj_ of _k_ clients is com­

piled and distributed to the chosen clients in the subsequent round as the global model.


The above outlined step 3 to 5 is repeated until the loss function reaches convergence.

### **2.2 Categories of federated machine learning**


Figure 2 illustrates the classification of FL, primarily based on three key aspects. Firstly
based on the system’s architecture, FL is categorized primarily into two approaches: central­
ized and decentralized. In a centralized approach, a central entity serves as the orchestrator
in the client–server fashion. The central server is in charge of selecting clients for model
training, creating a global model by merging local models, and broadcasting model param­
eters to all parties. It is also termed synchronous FL architecture, whereas, in the decentral­
ized approach, there is no separate role as server or client. At each round, each client can
aggregate the local models and broadcast global model parameters to other clients.

Secondly, FL systems fall into two categories: cross-device and cross-silo, based on the
federation scale. Cross-device FL systems consist of numerous mobile devices with diverse
processing capabilities, where the model is distributed to the clients and trained on local
data during the training phase (Gabrielli et al. 2023). Nevertheless, FL is equally effec­
tive in cross-silo scenarios, in which the number of federated clients is often low, but their
computing capacity is high, typically found in large enterprises or data centers. In this, the

# `1 3`


Exploring privacy mechanisms and metrics in federated learning


**Fig. 2** Categories of federated learning


**Fig. 3** Distribution of data across sample and feature spaces



Page 7 of 51 **223**



local model is combined with data centers and data silos to create a global model. Lastly,
FL is divided into three types based on how data is distributed across the sample and fea­
ture spaces: horizontal federated learning, vertical federated learning, and federated transfer
learning, as shown in Fig. 3.


- **Horizontal federated learning:** Datasets are split horizontally based on the user di­

# `1 3`


**223** Page 8 of 51



D. Shenoy et al.



mension, and the part of the datasets that has similar user attributes but differs in users
is considered for training.

- **Vertical federated learning:** Datasets are split vertically based on the user feature di­
mension, and the part of the datasets that have similar users but differing user attributes
is considered for training.

- **Federated transfer learning:** Sample and feature spaces of two datasets differ and the
transfer learning is utilized to overcome the absence of data or tags.

### **2.3 Aggregation algorithms**


In FL, aggregation algorithms are essential for combining updates from multiple decentral­
ized clients into a global model without sharing raw data. These algorithms ensure that the
global model improves over time while maintaining the privacy of individual clients. Every
training iteration in FL is as illustrated in Fig. 4, where a global model update is obtained by
aggregating all of the local model updates.

There are several aggregation approaches like average aggregation, where server aver­
ages the received updates from clients, clipped average aggregation which is similar to
average aggregation, but outlier updates are clipped to reduce the influence of malicious
or abnormal updates. Momentum aggregation helps to speed up model convergence and
bayesian aggregation uses inference to account for uncertainty in client updates, improving
model generalizability. Secure aggregation incorporates cryptographic methods to ensure
client data privacy where as quantization aggregation reduces the size of model updates by
compressing them before transmission to enhance efficiency. Hierarchical aggregation is
performed at multiple hierarchical levels, reducing communication overhead and personal­
ized aggregation considers the specific data characteristics of each client, ensuring personal­
ized updates. Table 2 and 3 lists the widely used aggregation algorithms and strategies in FL
and highlights its outcomes.

### **2.4 Benchmark framework details**


Federated Learning benchmark frameworks are essential for progressing the field of FL
and facilitating the creation of effective, efficient, and robust FL algorithms. They offer
standardized datasets, metrics, and protocols, allowing researchers to assess and compare
various FL algorithms in consistent environments. This enables a comprehensive evaluation


**Fig. 4** Training process in federated learning

# `1 3`


Exploring privacy mechanisms and metrics in federated learning



Page 9 of 51 **223**

# `1 3`


**223** Page 10 of 51

# `1 3`



D. Shenoy et al.


Exploring privacy mechanisms and metrics in federated learning


**Table 4** Federated learning framework details



Page 11 of 51 **223**



Framework
name

TensorFlow
Federated



Developer Source Description



TensorFlow Google [https://github.com/tensorflow/federated](https://github.com/tensorflow/federated) Supports training
Federated and evaluation

with high level
interface FL API and
developing novel FL
algorithms with low
level interface called
FL core
PySyft OpenMind [https://github.com/OpenMinded/PySyft](https://github.com/OpenMinded/PySyft) Supports user to per­
form deep learning
with PyTorch
Flower Project by Ox­ [https://flower.dev/](https://flower.dev/) Supports hetero­
ford, UCL and geneous devices
Cambridge and known for



Flower Project by Ox­ [https://flower.dev/](https://flower.dev/) Supports hetero­
ford, UCL and geneous devices
Cambridge and known for

scalabality
OpenFL Intel Corp [https://github.com/](https://github.com/) Supports multiple
aggregation algo­
rithms like FedAvg,
FedProx, FedOpt etc
PaddleFL Baidu [https://github.com/PaddlePaddle/PaddleFL](https://github.com/PaddlePaddle/PaddleFL) Supports both
DP and MPC and
capable of working
with honest-butcurious parties
FedML Univer­ [https://github.com/FedML-AI/FedML](https://github.com/FedML-AI/FedML) Supports standalone,
sity of Southern distributed and onCalifornia device training simu­



FedML Univer­ [https://github.com/FedML-AI/FedML](https://github.com/FedML-AI/FedML) Supports standalone,
sity of Southern distributed and onCalifornia device training simu­

lation environment
Federated WeBank [https://github.com/FederatedAI/FATE](https://github.com/FederatedAI/FATE) Supports industrial
AI Technol­ level FL services
ogy Enabler between different
(FATE) organizations



Federated WeBank [https://github.com/FederatedAI/FATE](https://github.com/FederatedAI/FATE) Supports industrial
AI Technol­ level FL services
ogy Enabler between different
(FATE) organizations

Fed-BioMed French Computer [https://github.com/fedbiomed](https://github.com/fedbiomed) Supports biomedical
Institute INRIA research and is user



Fed-BioMed French Computer [https://github.com/fedbiomed](https://github.com/fedbiomed) Supports biomedical
Institute INRIA research and is user

friendly to deploy
XayNet Xayn-Berlin [https://github.com/xaynetwork/xaynet](https://github.com/xaynetwork/xaynet) Supports horizontal
and transfer learn­
ing FL
IBM Feder­ IBM Watson [https://ibmfl.mybluemix.net/github](https://ibmfl.mybluemix.net/github) Supports training of
ated Learning Project neural networks and



IBM Feder­ IBM Watson [https://ibmfl.mybluemix.net/github](https://ibmfl.mybluemix.net/github) Supports training of
ated Learning Project neural networks and

decision trees
MATLAB MathWorks [https://www.mathworks.com](https://www.mathworks.com) Offers the tools
necessary to enable
distributed and de­
centralised machine
learning network
training and uses
MATLAB objects to
simulate device dis­
tributed computing



IBM Watson
Project


# `1 3`


**223** Page 12 of 51


**Table 4** (continued)



D. Shenoy et al.



Framework
name



Developer Source Description



NVIDIA
CLARA



NVIDIA NVIDIA ​h​t​t​p​s​: **​** /​/​d​e​v **​** e​l​o​p​e​r **​** .​n​v​i **​** [d​i​a​.​c](https://developer.nvidia.com/blog/federated-learning-clara/) **​** o​m​/​b​l **​** o​g​/​f​e​d **​** e​r​a​t **​** e​d​-​l​e​ AI healthcare solu­
CLARA [a​r​n​i​n​g​-​c​l​a​r​a​/](https://developer.nvidia.com/blog/federated-learning-clara/) tions and software

services ranging
from imaging to
genomics and drug
development
Substra Owkin [https://arxiv.org/abs/1910.11567](https://arxiv.org/abs/1910.11567) Uses distributed
learning, sharing
algorithms, predic­
tive models, and
distributed ledger
technology for cal­
culations, ensuring
information validity
and traceability



NVIDIA ​h​t​t​p​s​: **​** /​/​d​e​v **​** e​l​o​p​e​r **​** .​n​v​i **​** [d​i​a​.​c](https://developer.nvidia.com/blog/federated-learning-clara/) **​** o​m​/​b​l **​** o​g​/​f​e​d **​** e​r​a​t **​** e​d​-​l​e​
[a​r​n​i​n​g​-​c​l​a​r​a​/](https://developer.nvidia.com/blog/federated-learning-clara/)



of FL algorithms regarding accuracy, communication efficiency, and computational costs.
Common framework promotes collaboration among researchers, practitioners, and orga­
nizations by aligning their efforts toward shared goals and challenges. In this section, we
highlight the benchmark frameworks as shown in Table 4 which is used to conduct various
experiments in FL.

### **2.5 Challenges in federated learning**


To effectively safeguard user privacy, federated learning faces several challenges that need
to be addressed, which are outlined below:


- **Privacy protection:** FL protects the privacy of each device by sharing model gradients
with the server rather than the raw data. It is important to make sure that no personal
information is disclosed by the federated learning training model. There have been de­
velopments in data privacy techniques resulting in increased computation complexity
and computational load on the federated network.

- **Communication cost:** One major obstacle in federated learning is communication. A
federated network is made up of millions of dispersed mobile devices and learning
model may require a lot of communication during training data sharing among them.
Techniques with high communication efficiency must be developed by considering the
communication cost of FL, given the unreliability of network connection speed. If not
adequately handled, the frequency of model updates might also overload the network.

- **System heterogeneity:** The federal environment has a lot of edge devices, and few
devices may include Non-IID (non-independent and identically distributed) data. Be­
cause of variations in hardware, software, and network connectivity, every client in the
network may have varying computation and communication capabilities. Some devices
in a network are concurrently active, while others occasionally get the connectivity.
Therefore, FL techniques must be resilient to offline devices in the network and able to
withstand diverse hardware. Techniques for data formatting and storage can also differ
throughout devices.

# `1 3`


Exploring privacy mechanisms and metrics in federated learning



Page 13 of 51 **223**




- **Unreliable model upload:** Potential risks to FL stem from mobile nodes that could
intentionally or unintentionally aggregate the global model, potentially resulting in er­
rors throughout the model’s training process. Low-quality model uploads can also result
from unstable mobile network conditions. Appropriate authentication methods must be
implemented at the data sources and aggregation servers due to security issues in FL
related to data and model poisoning. Large-scale poisoning attacks could affect the ac­
curacy of the model. Due to the model’s centralization approach, distributed denial of
service (DDoS) attacks can also affect the network and central server. Frequent updates
to local models and the data they gather into the global model may overburden the net­
work and central server.

## **3 Privacy preserving federated learning**


This section discusses the importance of privacy protection and critical technologies in FL,
reviews literature on strategies to preserve privacy and advance research in the chosen area.
It enables us to recognize the possible advantages and restrictions of every privacy protec­
tion system and how it might be used to handle data privacy issues in situations involving
collaborative learning.

### **3.1 Need for privacy preservation in federated learning**


Studies on federated learning have proposed different solutions to implement privacyenhancing schemes with the aim of addressing privacy in FL. The primary challenges that
arise and require attention are as follows:


- **Gradient information leakage:** Gradient inference could reveal user training samples.
Research indicates that participant data may not be protected when transferring gra­
dients. Private training data can still be exposed through the shared gradient updates
(Zerka et al. 2020).

- **Unreliable participants or clients:** Malicious users might submit specially crafted gra­
dients during the training phase, which could undermine the model’s availability and
integrity. To stop the model from converging or tamper with the global model, they can
conduct targeted and untargeted poisoning attacks (Mothukuri et al. 2021b).

- **Honest but curious server:** Though only gradients are sent as training results to the
server for aggregation, the honest-but-curious servers might retain information about
the training data provided by users through model update gradients (Le et al. 2023).

- **Direct release of trained Model:** An attacker can successfully obtain the nodes’ private
information from trained models by eavesdropping on the communication channel and
compromising the FL system (Wang et al. 2023a).

### **3.2 Privacy enhancing technologies**


In order to improve FL’s privacy preservation during the training process, numerous
approaches have been suggested as shown in Fig. 5.

# `1 3`


**223** Page 14 of 51


**Fig. 5** Privacy-preserving schemes

### **3.2.1 Anonymization**



D. Shenoy et al.



The dataset contains personal information about an individual, including medical diagno­
sis records, bank account details, etc., and with the release of such a dataset, the personal
information can be compromised. One of the effective techniques for safeguarding data
privacy is anonymization. It is the process of eliminating any personal information that
could be used to identify a particular user. However, some case studies show these opera­
tions are insufficient to guarantee the privacy of private information, especially with respect
to statistical queries. Choudhury et al. (2020), a syntactic strategy for anonymizing local
data at each site, which is ( _k, km_ )-anonymity-based approach. It considers both relational
and transactional attributes of local healthcare data _Di_ from _N_ sites, where _k_ represents the
minimum number of records in a group that must be indistinguishable from one another and
_km_ refers to the minimum number of distinct values for a sensitive attribute within each
group of _k_ records. The function _uR_ () and _uT_ () measures information loss for relational and
transactional attributes, with parameter _δ_ balancing the goals, where _uR_ is the utility of the
relational part and _uT_ is the utility of the transactional part.

### **3.2.2 Differential privacy**


It is a technique developed to facilitate secure analysis over sensitive data to safeguard
privacy. It guarantees that the addition or exclusion of a single record from a dataset has no
effect on the outcome of an algorithm (Dwork 2008). By injecting noise or using a sample
dataset, it maintains statistical features and safeguards the privacy of the data by preventing
an attacker from getting the exact information even after several searches. It is usual practice
to add noise into output during each iteration phase in order to maintain user privacy (Abadi

# `1 3`


Exploring privacy mechanisms and metrics in federated learning



Page 15 of 51 **223**



et al. 2016). FL can withstand membership inference attacks and prevent leakage of weights
during training by implementing DP mechanisms. DP is implemented by introducing noise
to the data with two different approaches, namely: Sensitivity of a function explained in
work (Dwork 2006) and based on an exponential distribution among discrete values given
in work (McSherry and Talwar 2007). The sensitivity of a function _M_ : _D_ _→_ _R_ _[d]_ over an
arbitrary domain for datasets, say, _D_ and _D_ _[′]_ differing by a record is the maximum change in
the output of _M_ over all possible inputs.


_M_ = _maxD,D′_ _M_ ( _D_ ) _M_ ( _D_ _[′]_ )
_∥_ _−_ _∥_

The probability distribution induced by a quality function _q_ : ( _D_ _[n]_ _× R_ ) _→_ _R_ for given dataset
_dϵD_ _[n]_, assigns a score to each outcome _rϵR_ by _S_ ( _q_ ) = _maxr,D,D_ _[′]_ _q_ ( _D, r_ ) _q_ ( _D_ _[′]_ _, r_ )
_∥_ _−_ _∥_
. The mechanism _M_ to choose an outcome _rϵR_ given a dataset instance _dϵD_ _[n]_ is defined as



_ϵ·q_ ( _d,r_ )

~~2~~ _S_ ~~(~~ _q_ ~~)~~



, where _α_ serves as a normalization



)



_M_ ( _d, q_ ) = return _r_ with probability _α ·_ exp



(



factor.



By introducing partial noise, DP protects against computationally dominant adversar­
ies. The findings are greatly impacted by the phenomenon of introducing noise to a small
dataset during model training. DP is, therefore, not appropriate for small datasets and is best
suited for large datasets. Thus, DP algorithms introduce noise to the dataset and secure the
data by preventing re-identification attacks. Two noise-adding differential privacy methods
are Gaussian and Laplacian methods (Wu et al. 2022). Further, based on the application
scenario, DP can be of two types, namely global differential privacy and local differential
privacy. Both forms of DP can ensure _ϵ−_ differential requirements of an individual user,
regardless of minor differences in application scenarios. Clients employ a federated optimi­
zation technique with DP (Geyer et al. 2017) to safeguard their global differential privacy.
The trained model contains a large number of parameters to ensure accurate communica­
tion and functionality. The increase in noise will greatly diminish its validity. A method
based on adaptive gradient is suggested (Andrew et al. 2021) to reduce noise penetration to
the gradient and avoid adding unnecessary extraneous noise. DP is used in FL healthcare
applications as an essential privacy algorithm, mainly to train medical text data and develop
models for medical image analysis (Adnan et al. 2022). Lu et al. (2022) suggested appropri­
ate deep learning models may be efficiently developed from distributed data warehouses
via poorly supervised multi-pose learning with DP by using random noise, avoiding the
complication of direct data sharing. Data privacy for diabetic patients is protected (Chang
et al. 2021) by the development of an adaptive differential privacy algorithm and a gradient
verification-based consensus technique to detect poisoning attacks. An incentive mecha­
nism using differential private federated learning (DPFL) framework (Wang et al. 2021a)
developed to prevent data owners’ privacy leakage in the IoT domain. DP provides a notably
higher level of protection with some degree of precision loss. The study (Gupta et al. 2022b)
addresses the challenge of noise injection by dividing data into sensitive and non-sensitive
parts, enhancing the efficiency of the classification model. It aims to ensure data security
during storage, analysis, and sharing in the cloud while maintaining utility and minimizing
computational overhead. To achieve this, the data is partitioned using k-anonymization, and
privacy is preserved by selectively applying the Laplace mechanism to inject noise, thereby
reducing the impact of perturbation. The processed data is then shared with the cloud, where


# `1 3`


**223** Page 16 of 51



D. Shenoy et al.



a multi-layered feed-forward deep neural network (MFNN) is employed for analysis. The
MFNN is trained using the TriPhase Adaptive Differential Evolution (TADE) algorithm,
which optimizes the network’s learning process for improved performance.

### **3.2.3 Homomorphic encryption**


It is a cryptographic method that enables computations to be carried out on encrypted
data without decrypting it. HE is particularly useful in privacy-preserving data analytics,
as it allows untrusted parties to work on encrypted values without accessing the under­
lying plaintext data (Li et al. 2020c). There are different types of homomorphic encryp­
tion schemes, such as partially homomorphic encryption (PHE), somewhat homomorphic
encryption (SWHE) and fully homomorphic encryption(FHE). PHE helps to protect sensi­
tive data confidentiality by allowing only specific mathematical operations to be carried out
on encrypted information. With this method, a ciphertext can undergo a single operation an
infinite number of times (Wu et al. 2021b). SWHE schemes are limited to a finite number of
operations (such as addition or multiplication) and a finite number of executions (Chen et al.
2021). FHE scheme enables computations on both addition and multiplication operations
and supports any number of operations (Gao et al. 2023a), and improving the efficiency of
secure multi-party computation. It can handle arbitrary computations of ciphertexts. Addi­
tively homomorphic encryption (AHE) allows for the computation of encrypted messages,
such as adding two encrypted messages or adding an encrypted message to a clear integer.
One popular example of AHE is the Paillier cryptosystem that satisfies the basic homo­
morphic properties, such as the ability to perform addition and scalar multiplication on
ciphertexts. It uses a modulus and a random generator to encrypt messages and perform
computations on them (Madi et al. 2021). Additive homomorphism is employed to guaran­
tee model parameter sharing security, preventing the central server from leaking the client’s
private information (Aono et al. 2017). While federated logical regression model (Hardy
et al. 2017), employs an additive homomorphism method to effectively resist honest and
curious attackers, the SecureBoost (Cheng et al. 2021) a decision tree model is constructed
using HE to protect its parameters.

ElGamal encryption has multiplicative homomorphic property based on Diffie-Hellman
key exchange, allowing for operations on ciphertexts without decrypting them (Li et al.
2023b). Linearly homomorphic encryption (LHE) is a public-key encryption scheme that
supports linearly homomorphic operations over ciphertexts (Liu et al. 2021). It enables
computations to be carried out directly on encrypted data without needing access to the
secret key. The semantic security property of LHE ensures that an adversary cannot distin­
guish between ciphertexts and random values. FHE satisfies the requirement that operations
on plaintext that involve addition or multiplication be equal to those that involve ciphertext.
It enables computations on sensitive data while preserving privacy, as the data remains
encrypted throughout the computation process. FHE schemes, such as the Cheon-Kim-KimSong (CKKS) scheme, can handle operations on floating-point numbers and vectors (Miao
et al. 2022).

Zhang et al. (2019) safeguards gradients on an untrusted server by encrypting par­
ticipants’ local gradients using the Paillier homomorphic cryptosystem, thereby ensuring
secure aggregation and protecting individual privacy. Zhang et al. (2023) proposed a FL
scheme that consists of a Paillier homomorphic cryptosystem combined with an online or

# `1 3`


Exploring privacy mechanisms and metrics in federated learning



Page 17 of 51 **223**



offline signature technique that enables the edge server to safely handle the offline portion
of the lightweight gradients integrity verification for edge computing systems. To address
attacks aiming at security vulnerabilities, Jia et al. (2021) proposed a data protection aggre­
gation scheme based on distributed random forest with DP and the distributed AdaBoost
with HE methods, which enable multiple data protection in data sharing and model sharing
and integrated the methods with Blockchain and FL. To accomplish ciphertext-level model
aggregation and model filtering, Wang et al. (2022a) proposed FL for internet of vehicles
(IoV) which can enable the verifiability of the local models while maintaining privacy using
multi-krum technology fused with AHE. Authors (Miao et al. 2022) proposed a Blockchaininfused FL to reduce the poisoning attacks from malicious clients and servers. They use
cosine similarity to detect malicious gradients uploaded by malicious clients and employ
CKKS and FHE to guarantee the privacy and integrity of the federated learning process
through secure aggregation. In order to prevent attacks, Sun et al. (2022) proposed a Block­
chain FL audit strategy for encrypted gradients. The strategy records the encrypted gradients
from data owners using a behavior chain, and it assesses the quality of the gradients using
an audit chain. The scheme uses a homomorphic noise mechanism that guarantees the avail­
ability of aggregated gradient.

### **3.2.4 Secure multiparty computation**


It is a method for processing sensitive data while maintaining privacy. It permits several par­
ticipants to collaboratively calculate a function on their confidential inputs without disclosing
any details about their inputs to one another. SMPC can be used to accomplish secure aggre­
gation of local model updates while maintaining privacy in FL. One of the most important
methods in information security is cryptography, which is essential to safeguarding data in
all its forms. It accomplishes this by providing technological and theoretical support for data
integrity, privacy, and authentication. Segmentation between encrypted data sharing is made
possible by secure multiparty computing, preventing any client from retrieving the complete
dataset by themselves. The evolution of SMPC yields three modules: oblivious transfer,
secret sharing, and garbled circuits. Semi-honest adversary, malicious adversary, and covert
adversary models are three main categories of SMPC security models. SMPC plays a crucial
role in safeguarding privacy in FL for medical applications (Li et al. 2023a). Pairwise mask­
ing and secret sharing are the main techniques used by SMPC. The clients agree on pairwise
masks using the Diffie-Hellman key exchange in pairwise masking. However, the aggrega­
tion result may not provide sufficient privacy for individual users. AHE techniques can be
used to protect each uploaded local value after masking, but it incurs extra overhead. While
the secret-sharing technique involves dividing the secret into several parts and distributing
it to several parties, and the technique expects participants to combine their shares in order
to put the secret back together. Using technique of secret sharing, several privacy-preserving
aggregation techniques in FL do not require any additional steps before delivering masked
data. Chain-PPFL (Li et al. 2020c) maintains privacy based on the chained secure multiparty
computing approach, which employs two techniques: the single-masking mechanism and
the chained communication mechanism. These enable clients to transmit masked informa­
tion through a sequential chain framework. Group signature for federated learning (GSFL)
scheme (Kanchan et al. 2023) is a privacy-preserving protocol that lowers compute and
transmission costs dramatically while safeguarding client identity and data privacy from

# `1 3`


**223** Page 18 of 51



D. Shenoy et al.



privacy-related threats using HE and a group of clients can be validated using the group
signature creation based on zero-knowledge proof of knowledge (ZKPoK).

AnoFel Almashaqbeh and Ghodsi (2023) allows users to remain anonymous while engag­
ing in a dynamic environment that will enable users to join and exit at any time. SVeriFL
(Gao et al. 2023a) uses Boneh–Lynn–Shacham (BLS) signature and multiparty security to
verify parameter integrity, server aggregation results, and participant data privacy. HyFL
hybrid framework (Marx et al. 2023) that uses SMPC techniques and hierarchical feder­
ated learning to ensure data and global model privacy, facilitating large-scale deployments
and reducing workload on resource-constraint devices. MLGuard (Khazbak et al. 2020), a
distributed earning system that protects privacy and mitigates poisoning attacks using an
additive secret sharing scheme, multiplication triplets, and cosine similarity score. BFLC
(Li et al. 2020b), a Blockchain-enabled FL with a committee consensus mechanism, ensures
global model stability, addresses storage capacity and consensus efficiency challenges, and
uses _k_ -fold cross-validation and anti-malicious smart contracts to elect superior nodes for
training rounds. Biscotti (Shayan et al. 2021), a Peer-to-Peer (P2P) approach that uses
Blockchain and cryptographic primitives to coordinate ML processes between peering cli­
ents. It uses the multi-krum defense to stop peers from poisoning the model, differentially
private noise to offer privacy, and Shamir secrets to securely aggregate SGD updates. Using
zero-knowledge non-interactive arguments of knowledge (ZK-NIARK) and lightweight
primitives like pseudorandom generators, (Wang et al. 2023b) suggests a PTDFL solution
to handle data owners’ access and privacy problems in decentralized FL.

## **4 Architectures of federated learning**


The literature on privacy-preserving mechanisms in FL falls into two main categories based
on their architecture, namely, centralized FL and decentralized FL. Figure 6 depicts the lit­
erature map of the FL architecture-based studies covered in this section. This map includes


**Fig. 6** Literature map based on FL architectures (Litmaps 2024)

# `1 3`


Exploring privacy mechanisms and metrics in federated learning



Page 19 of 51 **223**



circles representing articles of different numbers of citations, which are grouped based on
two colors to differentiate between centralized and decentralized FL. The primary privacy
mechanism used in FL is a combination of privacy-preserving technologies like DP and
SMPC with HE, secret sharing, or pairwise masking techniques.

### **4.1 Privacy preserving centralized federated learning**


This section discusses the literature review on privacy mechanisms in a centralized FL envi­
ronment where aggregation of gradients is done by a dedicated central server. In the subse­
quent literature (Liu et al. 2021; Kanchan et al. 2023; Wang et al. 2023a; Gao et al. 2023a),
the privacy-preserving FL framework uses HE as the core privacy-enhancing technology.
Liu et al. (2021) uses LHE that performs computations on encrypted gradients without
accessing the actual user data to safeguard user privacy. It computes the Pearson correlation
coefficient using coordinate-wise medians to identify malicious gradients and subsequently
detect poisoning attacks. Kanchan et al. (2023) incorporates HE and enables the aggregation
of encrypted data on the server. It includes a masking technique to mask individual client
data, and a group signature is used to validate a group of clients. This scheme simultane­
ously ensures non-repudiation, privacy, and authentication. Paillier HE is used to encrypt
user training models on the client side, prevents internal attacks through access control, and
deploys an acknowledgment mechanism at the server to temporarily remove unresponsive
users to reduce waiting delays and communication overhead (Wang et al. 2023a). Gao et al.
(2023a) focuses on confirming the accuracy of the aggregated result using multi-party secu­
rity with BLS signature, as well as the integrity of the uploaded parameters to the server,
utilizing CKKS approximation homomorphic encryption for data privacy.

By retaining a clean, limited training dataset, the service provider builds confidence (Cao
et al. 2020), a byzantine resilient federated learning technique. A server model is used to
validate local model changes, effectively limiting the impact of malicious local models.
It proposed a new aggregation rule based on ReLU-clipped cosine similarity-based trust
score that considers the direction and amount of local model updates in addition to server
model update. Multi-hop communication with BLS signature is used to hide clients’ iden­
tities (Karakoc et al. 2023). It prevents malicious activities by forwardee clients altering
model updates and proposed approaches for robustness against packet drop behaviors. This
allows the server to perform certain analyses without violating privacy and prevents leak­
age of model updates. FL frameworks that focus on privacy-preserving and also reduce
the communication load that is incurred due to repeated exchange of model updates by
use of quantization technique are explored in literature (Nguyen et al. 2022; Nagy et al.
2023; Lang et al. 2023a, b). Nguyen et al. (2022) seeks to maximize the convergence rate
and meet differential privacy needs by optimizing quantization with binomial mechanism
parameters and communication resources. Bitwise quantization is applied to local model
updates, followed by the update vector’s application of the randomized response method of
local differential privacy (LDP) and the encoding of client-side text input using the rolling
feature hashing technique (Nagy et al. 2023). In Lang et al. (2023a), a random codebook is
used to encode data with the help of a nested lattice quantizer and generate a set of model
updates with a few bits that are applied with the randomized response mechanism of LDP.
The bits received in this system are converted into an empirical histogram throughout the
decoding process. Random lattice vector quantization (Lang et al. 2023b) is used, which is a

# `1 3`


**223** Page 20 of 51



D. Shenoy et al.



compression technique that helps to enhance privacy by incorporating multivariate privacy
preserving noise at a desired bit-rate without significantly affecting the model’s utility.

FL framework that implements SMPC techniques ensures data and global model privacy.
Serial chain framework is employed to enable the transfer of masked data among partici­
pants using the chained SMPC method (Li et al. 2020c). The hybrid framework approach
(Marx et al. 2023) ensures data and global model privacy through hierarchical FL and
SMPC techniques, thereby decreasing the workload on resource-constrained devices and
enabling large-scale deployments. This scheme prevents malicious clients from executing
model poisoning attack. There are privacy-preserving frameworks in FL (Zhao et al. 2021a;
Wu et al. 2022; Le et al. 2023; Almashaqbeh and Ghodsi 2023) that use DP to protect cli­
ent’s privacy. They eliminate malicious clients that affect the prediction of global model
accuracy. Zhao et al. (2021a) reduces privacy leakage by sharing only a few parameters and
utilizes a proxy server to ensure client anonymity. It applies DP with a Gaussian mechanism
to guarantee strong privacy protection. Wu et al. (2022) aims to limit the communication
cost and provide stronger privacy preservation by combining an adaptive gradient descent
technique like adam and adabound algorithm and uses DP with Gaussian mechanism. Adap­
tive differential privacy (Le et al. 2023) is used for local model updates and DP-tolerant
anomaly detection (DPAD) technique confirms abnormalities. Almashaqbeh and Ghodsi
(2023) maintains user anonymity while allowing users to join and exit, using non-interac­
tive proofs and cryptographic promises. Tables 5 and 6 summarizes the existing literature of
centralized federated learning with privacy technique implemented along with model and
dataset details used in the experimentation.

### **4.2 Privacy preserving decentralized federated learning**


Decentralized federated learning (DFL) emerged as a new method in response to the two
main problems with centralized FL, namely single point of failure (SPoF) and denial of
service (DoS) attack. An alternate to the conventional client–server architecture is provided
by P2P computing (Barkai 2000). While P2P distributed systems promote decentralization
by allowing network nodes to act as both servers and clients, client–server architectures
involve requests being made by clients and central servers processing and responding to the
requests. Here, nodes form a network by randomly joining each node into a chain. All nodes
are equal; there is no node more or less significant than each other. Peers should also act as
both clients and servers by providing services. Peers have to be self-sufficient and decide for
themselves how much they want to participate in the network.

With the help of a private interplanetary file system (IPFS), Pappas et al. (2021) offers
a fully decentralized system that enables any client to initiate or participate in an ongoing
training process. This framework requires less resources, grows with the number of par­
ticipants, and is resilient to sporadic connectivity. It also manages participant arrivals and
departures dynamically. The decentralized architecture (Zhao et al. 2022) guarantees safe
deep learning model training with a cipher-based matrix multiplication algorithm that is
efficient and verifiable. The foundation of the trusted DFL algorithm (Gholami et al. 2022)
is the concept of trust between network entities collaborating to accomplish specific goals.
Information from previous collaborations is used to establish and preserve this trust. Trust
estimations play a critical role in decisions about resource allocation, agent involvement,
and access control. A consensus problem involves considering both local and global trust

# `1 3`


Exploring privacy mechanisms and metrics in federated learning



Page 21 of 51 **223**



**Table 5** Summary of existing literature of centralized federated learning
Paper Methodology Privacy Limitation Models Dataset
technique



Cao
et al.
(2020)



Cao Trust bootstraping by ser­ ReLu clipped Poisoned root CNN & MNIST
et al. vice provider and restricts cosine similarity dataset can fail ResNet20 (0.1, 0.5,
(2020) the effects of malicious FLTrust scheme architecture Fashion,

local model CH),

CIFAR 10
and human
activity
recognition
Li et al. Incorporates single-mask­ Secure multipar­ Formation of CNN, MLP, MNIST
(2020c) ing and chained-commu­ ty computation chain and selec­ and L-BFGS and



Trust bootstraping by ser­
vice provider and restricts
the effects of malicious
local model



ReLu clipped
cosine similarity



Poisoned root
dataset can fail
FLTrust scheme



CNN &
ResNet20
architecture



Incorporates single-mask­
ing and chained-commu­
nication mechanism



Secure multipar­
ty computation



Formation of
chain and selec­
tion of neighbour
nodes may
need additional
computations and
communication



CNN, MLP,
and L-BFGS



MNIST
and
CIFAR
100



Liu
et al.
(2021)


Mo
et al.
(2021)


Zhao
et al.
(2021a)


Madi
et al.
(2021)


Park
and Lim
(2022)


Wu
et al.
(2022)


Nguyen
et al.
(2022)



Performs computations
on encrypted gradients
and computes the pearson
correlation coefficient

To limit privacy leakages,
overcoming memory
limitations with greedy
layer-wise training

Reduce privacy leakage
by sharing fewer param­
eters and employs a proxy
server for anonymity

Framework leverages
Homomorphic Encryption
and Verifiable Computa­
tion approaches to im­
prove FL’s scalability and
performance in cross-silo
situations

Centralized server ag­
gregates encrypted local
model parameters without
decryption and enable
every node use a separate
HE private key

Provides stronger privacy
preservation by combin­
ing an adaptive gradient
descent technique

Seeks to maximise con­
vergence rate and meet
DP needs by optimising
quantization



Linearly
homomorphic
encryption


Trusted Execu­
tion Environ­
ments (TEEs)


DP with Gauss­
ian mechanism


Additive
homomorphic
encryption


Distributed
homomorphic
cryptosystem


DP with Gauss­
ian mechanism


Stochastic qlevel quantiza­
tion and DP
with binomial
mechanism



Gradients cor­
relation can
infer the client’s
information

Dishonest attacks
are not addressed


proxy server used
for anonymity can
be malicious


Does not support
cross-device
situation where
a larger number
of potentially
untrusted clients
are involved

Every client with
different private
key leads to
computational
overhead


Lacks convergence
efficiency


Mixed inte­
ger non-linear
programming
(MINLP) problem,
which may be
computationally
expensive



Two layer
fully connect­
ed network



MNIST
and
CIFAR 10



CNN MNIST
and
CIFAR 10


MLP and CNN MNIST



Deep learning
Models



FEMNIST




- 


Fadam and
Fadabound


3 layer Neural
Network



Not
mentioned


MNIST

# `1 3`


**223** Page 22 of 51


**Table 6** Summary of existing literature of centralized federated learning continued



D. Shenoy et al.



Paper Methodology Privacy
technique



Limitation Models Dataset



Le et al.
(2023)


Al­
mashaqbeh
and Ghodsi
(2023)


Marx et al.
(2023)



DPAD
algorithm
at server,
Adaptive DP
at client


Threshold
Paillier
encryption,
Zeroknowlegde
proof, DP
with gaussian
noise

SMPC using
secret sharing



Single point of
failure


Communication
overhead during
setup and training
phase


Lack of quantiza­
tion technique
for performance
improvements
with respect to
training



Logistic Regression
and CNN


LeNet5, ResNet20,
SqueezeNet



LeNet, ResNet9 MNIST
and CIFAR
10



MNIST
and CIFAR
10


MNIST,
CIFAR 10,
TinyIma­
geNet



Karakoc
et al. (2023)



Karakoc Prevents mali­ multi-hop Communication CNN Fashion
et al. (2023) cious activities by commu­ overhead due MNIST,

forwardee clients nication to multi-hop CIFAR 10
altering model with BLS communication and CIFAR
updates signature 100

Nagy et al. Bitwise quantiza­ Randomized- Slight reduction LSTM IMDB
(2023) tion on local response of in accuracy due movie re­



multi-hop
commu­
nication
with BLS
signature



LSTM IMDB
movie re­
views and
MovieLens



Randomizedresponse of
LDP



Communication
overhead due
to multi-hop
communication


Slight reduction
in accuracy due
to DP



Wang et al.
(2023a)



Wang et al. Encrypt user Paillier HE Poisoning at­ MobileNet, Shufflenet, CIFAR
(2023a) training models tacks can lead Squeezenet, Mnasnet, 10 and

at client side to ciphertext Resnet and Densnet APTOS
and temporarily explosion and blindness
remove unrespon­ model integrity detection
sive users destruction dataset

Lang et al. Use random co­ Randomized Additional com­ CNN MNIST
(2023a) debook to encode response putation involved and CIFAR



Paillier HE Poisoning at­
tacks can lead
to ciphertext
explosion and
model integrity
destruction



MobileNet, Shufflenet,
Squeezenet, Mnasnet,
Resnet and Densnet



Randomized
response
LDP



CNN MNIST
and CIFAR
10



Gao et al.
(2023a)

# `1 3`



Protect client’s
privacy while
eliminating
malicious clients
which can effect
the prediction
of global model
accuracy

Scheme supports
anonymity of
clients participat­
ing in dynamic
environment


Ensure data and
global model
privacy, facilitat­
ing large-scale
deployments and
reducing work­
load on resourceconstraint devices

Prevents mali­
cious activities by
forwardee clients
altering model
updates

Bitwise quantiza­
tion on local
model updates
and rolling hash
based feature
representation

Encrypt user
training models
at client side
and temporarily
remove unrespon­
sive users

Use random co­
debook to encode
data with nested
lattice quantizer


Focuses on
confirming the
accuracy of the
aggregated result
and the integrity
of the parameters
uploaded to the
server



BLS Signa­
ture, CKKS
approximate
HE



Additional com­
putation involved
in formation of
codebooks and
encoding the data

Time needed
to generate the
proof increases
as the participant
count grows



CNN, DNN and MLP Fashion
MNIST
and
NSL-KDD


Exploring privacy mechanisms and metrics in federated learning


**Table 6** (continued)



Page 23 of 51 **223**



Paper Methodology Privacy
technique



Limitation Models Dataset



Lang et al.
(2023b)


Yan et al.
(2024)



Uses random
lattice vector
quantization and
incorporates mul­
tivariate privacy
preserving noise

To improve com­
munication effec­
tiveness and solve
security flaws like
interference at­
tacks and gradient
leakage



LDP with
Laplace
mechanism


Kafka for
message
distribution,
Diffie-Hell­
man and DP



Increased
computational
complexity


Aggregation pro­
tocol increases
computational
complexity and
computation cost



Linear Regression,
MLP and CNN


Kafka (client) with
Zookeeper(producer)
Architecture



MNIST
and CIFAR
10


MNIST
and CIFAR
10



assessments. Local agents maintain a posterior probability distribution over the parameters
of a global model (Wang et al. 2022b). The empirical assessments show that it is a useful tool
for managing non-IID data, especially when task complexity rises. Li et al. (2022) devel­
oped a technique that groups clients into comparable groups without requiring advanced
knowledge of the count of clusters required. It aims to provide improved personalisation
by offering a single global model for each cluster. It is a two-stage method that uses client
similarities to realize an adaptable topology and apply approaches like neighbor selection
and neighbor augmentation methods that reduce noisy neighbor estimations.

Li et al. (2021a) tackled the DFL issue in IoT systems, which uses mutual knowledge
sharing across nearby clients to lessen performance deterioration brought on by client
drift when data heterogeneity is present. This study outperforms baseline approaches like
FedAvg and FullAvg. GossipFL (Tang et al. 2022) focuses on efficient network resource
utilization in practical DFL configurations, incorporating a sparsification technique and a
gossip matrix-generating algorithm. During each communication round, the client can share
a highly compressed model with one peer and improve bandwidth resource utilization. Par­
ticipants manage two separate models: a proxy model that is publicly shared and a private
model (Kalra et al. 2023). The proxy model preserves the privacy of each individual par­
ticipant while facilitating effective information sharing between them without the use of a
centralized server. Improved privacy protection and less communication overhead are two
of the main benefits of the study. Gao et al. (2023b) utilizes LDP to balance model accuracy
and data privacy, which demonstrates effectiveness in avoiding low-quality updates and
supports dynamic group settings. Wang et al. (2023b) addresses data owners’ access and
privacy risks by using zero-knowledge non-interactive arguments of knowledge and light­
weight primitives like pseudorandom generators and Lagrange interpolation.

Federated learning, built on Blockchain technology, is one method for attaining FL
decentralization by utilizing Blockchain functionalities. The decentralization and reliabil­
ity of Blockchain technology have drawn a lot of interest in its application. It uses smart
contracts for secure model aggregation, guaranteeing safe and authentic model updates.
Blockchain-enabled decentralized FL can offer incentives to clients, encourage the integra­
tion of more clients, and reduce the computation and communication burdens on nodes.
This section of the literature review focuses on such decentralized FL schemes. BlockFL
(Kim et al. 2019), is a Blockchain-based, tamper-proof, secure distributed ledger-based

# `1 3`


**223** Page 24 of 51



D. Shenoy et al.



FL system that validates local training results and expands its federation to include unreli­
able devices on a public network. Although the scheme encourages the inclusion of more
devices with additional training samples, it needs to account for the extra delay introduced
by the Blockchain network. Blockchain-driven, aggregator-free, decentralized FL system
(Ramanan and Nakayama 2020) that uses smart contracts to manage model aggregation and
update processes in FL. Decentralized multi-community training framework (Zhou et al.
2020) enables byzantine resilience for distributed learning using a sharding mechanism.
Further, scheme introduced by Li et al. (2020b) to validate and aggregate local updates,
ensures the stability of the global model using _k_ -fold cross-validation, where committee
nodes validate the local updates. By utilizing the homomorphic hashing technique for vali­
dation and introducing verifiers to handle the verification workflow of miners in parallel (Li
et al. 2021b), improves the efficacy of model verification. Through the use of Blockchain
technology, participating clients can confirm the global model integrity and a transparent
ledger can be used to record model updates. Privacy-enhancing techniques like differential
privacy can be implemented via Blockchain to enhance privacy in FL. Ma et al. (2022), a
distributed learning paradigm that combines Blockchain technology with federated learning
to address privacy issues. It uses LDP with Gaussian noise to enhance privacy. Mahmood
and Jusas (2022) improves data privacy and security, preventing data and model poisoning
attacks and promoting good behavior among decentralized nodes in FL with Blockchain
using DP with Laplace mechanism and public key infrastructure (PKI). Along with DP,
multi-krum defense is used by Shayan et al. (2021) and Zhao et al. (2021b) to validate the
updates through miners and thus overcome poisoning attacks. Zhao et al. (2021b) also uses
Shamir secrets for secure aggregation, while Singh et al. (2022b) implements SMPC by
combining techniques like homomorphic encryption and differential privacy. Wang et al.
(2022a) uses additive HE with multi-Krum technology to achieve cipher model aggregation
and filtering, allowing both privacy preservation and the verifiability of local models. It also
takes into account malicious client and server scenarios.

Many FL schemes incorporate a secure multiparty mechanism and focus on distributed
storage requirements. Miao et al. (2022) utilizes a Blockchain system to ensure transpar­
ency in processes and enhance privacy by employing cosine similarity and CKKS fully
homomorphic encryption. Li et al. (2023b) utilizes ElGamal encryption and smart contracts
for secure self-aggregation and it investigates privacy leakage. It presents a gradient inver­
sion attack in order to obtain the original data from sign-based quantized gradients. Chen
et al. (2023) uses advanced encryption standard with EAX mode of operation (AES-EAX)
to protect information sharing among nodes and Abou El Houda et al. (2023), a global
cyber-security framework that integrates Blockchain, FL, SMPC, and software-defined net­
working (SDN) technologies to manage distributed denial of service attack collaboration,
ensuring efficiency, reliability, and privacy. Singh et al. (2023) uses distributed hash table
(DHT) at the cloud layer facilitates decentralized storage, while Blockchain handles data
authentication and validation. Decentralized P2P (Sezer et al. 2023) concept with event
and storage-based smart contracts having cryptographic primitives like digital signatures is
used with IPFS for authentication. Baucas et al. (2023) proposed an IoT platform using fog
technology, combining private Blockchain and FL, to address network flexibility and data
privacy issues in wearable IoT devices for predictive healthcare. Blockchain-enabled DFL
schemes with privacy-preserving techniques and consensus algorithms are summarized in
Table 7 and 8.

# `1 3`


Exploring privacy mechanisms and metrics in federated learning


**Table 7** Summary of existing literature of decentralized federated learning



Page 25 of 51 **223**



Paper Methodology Privacy
technique



Paper Methodology Privacy Limitation Models Dataset Blockchain/
technique Consensus

Kim Secure, tamper- Blockchain Needs to - - Proofet al. proof system driven by utiliz­ deal with of-Work
(2019) with distributed ing a consensus extra delay (PoW)



Secure, tamperproof system
with distributed
ledger to record
device scores
and models,
validating local
training results
and rewarding
new devices



Blockchain
driven by utiliz­
ing a consensus
mechanism



Needs to
deal with
extra delay
brought
on by the
Blockchain
network




- - Proofof-Work
(PoW)



Raman­
an and
Na­
kayama
(2020)


Zhou
et al.
(2020)


Li et al.
(2020b)


Shayan
et al.
(2021)


Li et al.
(2021b)


Zhao
et al.
(2021b)



Blockchaindriven,
aggregator-free,
decentralised FL
system that uses
smart contracts
for model
aggregation

Decentralised,
multi-commu­
nity training
framework to
enable byzantine
resilience for
distributed
learning

Validate and
aggregate
local updates,
guaranteeing the
stability of the
global model

Decentralized
P2P method
for multi-party
machine learning
that protects
privacy of model
updates

Introducing veri­
fiers to reduce
the verifica­
tion workflow
of miners by
enabling parallel
verification

FL system with
Hierarchical
crowdsourc­
ing for home
appliance
manufacturers



Blockchain
driven using
smart contract


Sharding
mechanism


_k_ -fold
cross-validation


Multi-krum, DP,
Shamir secrets


Homomorphic
hashing algo­
rithm, Merkle
tree


DP with Lapla­
cian Mechanism,
Multi-Krum



Computa­
tional burden
is enormous
when con­
sensus duties
handled by
nodes


Need to
maintain
numerous
Blockchain,
not beneficial
for model
sharing


Intolerable
verification
delay


Verification
process is
central­
ized and is
vulnerable to
SPoF


Verifiers and
miners can
be malicious


Possible
malicious
customers
submitting
incorrect
model
updates



Two layer
DNN



CNN MNIST Proof-ofAccuracy


CNN MNIST Consortium
with Algo­
rand, Proof
of Stake
(PoS)

# `1 3`



Taxi driver
revenue data



Private
Ethe­
reum with
Proof-ofAuthority




- - Committee
wise RingAllreduce


AlexNet FEMNIST FISCO
Block­
chain with
Committee
consensus



Logistic
Regres­
sion and
Softmax
classifiers



MNIST and
credit card
datasets



Distributed
ledger with
Proof-offederation
(PoF)


**223** Page 26 of 51


**Table 7** (continued)



D. Shenoy et al.



Paper Methodology Privacy
technique



Paper Methodology Privacy Limitation Models Dataset Blockchain/
technique Consensus

Miao Reduce the Cosine similarity Trusted Key Three layer MNIST and Ethereum
et al. vulnerability of and CKKS FHE Genera­ neural Fashion
(2022) poisoning at­ tion Centre network MNIST



Reduce the
vulnerability of
poisoning at­
tacks from both
malicious clients
and servers



Cosine similarity
and CKKS FHE



Trusted Key
Genera­
tion Centre
(KGC),
solver and
verifier can
have SPoF

High com­
putational
and energy
consumption



Three layer
neural
network



MNIST and
Fashion
MNIST



Ethereum



Ma et al.
(2022)



Distributed
learning para­
digm to address
privacy issues



LDP with gauss­
ian noise




- Fashion
MNIST and
CIFAR 10



Smart
contract



Despite blockchain’s inherent transparency and immutability, establishing trust across
different blockchain-FL systems is complex due to various interoperability and governance
issues. Blockchain-enabled federated learning introduces challenges from the perspectives
of trust and privacy like cross-chain communication and the establishment of mutual trust
among platforms can be complicated as different blockchain platforms utilize distinct con­
sensus mechanisms. This interoperability issue makes it difficult for various blockchain
networks to efficiently exchange data or collaborate on federated learning tasks (Ou et al.
2022). Literature (Cheng et al. 2024) contributes to the creation of a decentralized proto­
col that manages cross-chain interactions autonomously, eliminating the need for trust in
external parties. This approach reduces the risk of centralization, strengthens security and
provides an efficient solution for cross-chain interaction. Managing decentralized trust is
difficult because of conflicting incentives between stakeholders and the risk of sybil attacks,
where malicious nodes generate fake identities to exploit the system. These challenges can
weaken trust in the blockchain network, disrupt the fair allocation of trust, and compromise
the integrity of federated learning operations. Ensuring equal distribution of trust among all
participating nodes remains a challenge, especially in diverse environments. Reputationbased mechanisms can help by implementing trust scores and evaluating the reliability of
FL participants (Raza et al. 2024). Authors (Yakubu et al. 2024) offers an analysis of the
advantages and drawbacks of various consensus mechanisms, along with the security weak­
nesses they may exhibit.

## **5 Privacy preserving FL applications**


Privacy-preserved FL is a transformative approach to ML, enhancing privacy and address­
ing regulatory and ethical concerns by enabling collaborative training across several data
sources without requiring direct data exchange. This section explores the diverse applica­
tions of privacy-preserved federated learning, used in various fields to balance robust data
insights with stringent privacy requirements, and demonstrates its successful implementa­
tion of privacy-enhancing techniques. Figure 7 depicts the literature map of various FL
applications covered in this section. This map includes circles representing articles of dif­
ferent numbers of citations and is grouped based on three colors to differentiate between
applications like healthcare, IoT, and NLP.

# `1 3`


Exploring privacy mechanisms and metrics in federated learning


**Table 8** Summary of existing literature of decentralized federated learning continued



Page 27 of 51 **223**



Paper Methodology Privacy
technique



Paper Methodology Privacy Limitation Models Dataset Blockchain/
technique Consensus

Mahmood To promote DP with Single aggrega­ DNN Fashion Ethereum
and Jusas good behavior laplace tor used for MNIST with Proof
(2022) among decen­ mechanism implementation of Work



Mahmood To promote DP with Single aggrega­ DNN Fashion Ethereum
and Jusas good behavior laplace tor used for MNIST with Proof
(2022) among decen­ mechanism implementation of Work

tralized nodes (PoW)

Yang Prevent model Markov deci­ - CNN MNIST, PBFT
et al. tampering from sion process CIFAR 10, consensus
(2022) the malicious framework, Heartactivity



To promote
good behavior
among decen­
tralized nodes



DP with
laplace
mechanism



Single aggrega­
tor used for
implementation



DNN Fashion
MNIST



Prevent model
tampering from
the malicious
server




- CNN MNIST,
CIFAR 10,
Heartactivity



Communication
overhead lead­
ing into system
delays



PBFT
consensus



Wang
et al.
(2022a)



Wang Malicious cli­ Multi-krum, Communication CNN MNIST, Consortium
et al. ent as well as Paillier AHE overhead lead­ BelgiumTS, with PBFT
(2022a) server scenario ing into system EMNIST (Practical

is considered delays Byzan­
and adopts dis­ tine Fault
tributed parallel Tolerance)
verification consensus

Sezer Decentralized Digital signa­ Delay in NA ECS (Electro Ethereum
et al. peer-to-peer tures based on network due Chemical with proof(2023) with event and ECDSA to processing Sensors) data of-concept



Malicious cli­
ent as well as
server scenario
is considered
and adopts dis­
tributed parallel
verification



Markov deci­
sion process
framework,
Deep rein­
forcement
learning
(DRL)
algorithm

Multi-krum,
Paillier AHE



CNN MNIST,
BelgiumTS,
EMNIST



Decentralized
peer-to-peer
with event and
storage based
smart contract
and enhances
privacy



Digital signa­
tures based on
ECDSA



Delay in
network due
to processing
requests



NA ECS (Electro
Chemical
Sensors) data



Ethereum
with proofof-concept



Baucas
et al.
(2023)


Chen
et al.
(2023)


Singh
et al.
(2023)


Li et al.
(2023b)



Combine private
Blockchain
and FL to ad­
dress network
flexibility

Framework
reduce com­
putation and
communication
overhead, and
address model
tampering and
privacy leakage
risks

Address issues
like centraliza­
tion, privacy
preservation and
security

Demonstrate
recovery of
original data
from sign-based
quantized
gradients via a
gradient inver­
sion attack



Access
control and
cryptographic
structure


AES-EAX
encryption


Distributed
Hash Table
DHT


Elgamal
Encryption



Lacks adapt­
ability to diverse
wearable IoT
devices


Reconstruction
phase’s compu­
tation overhead
is proportional
to the size


Limited scal­
ability and not
appropriate for
large-scale


Scalability is not
addressed



CNN Smartphone
dataset from
UCI ML
repository



LeNetZhu MNIST Ethe­
reum with
ZKPoK

# `1 3`



Private
Blockchain


Blockchain
ledger


Ethe­
reum with
Proof-ofAuthority



CNN and
ResNet18



Fashion
MNIST and
CIFAR 10




- CIFAR
10 and
FEMINIST


**223** Page 28 of 51


**Table 8** (continued)



D. Shenoy et al.



Paper Methodology Privacy
technique



Paper Methodology Privacy Limitation Models Dataset Blockchain/
technique Consensus

Wahrstät­ Framework de­ collateral- Scalability, gas Ethereum MNIST and Ethereum
ter et al. signed to foster backed repu­ costs, and data­ Ropsten CIFAR-10 Blockchain
(2024) trust in DFL tation system, set complexity testnet



Framework de­
signed to foster
trust in DFL
which addresses
scalability,
latency, and pri­
vacy issues



collateralbacked repu­
tation system,
keccak256hashed string
representation



Scalability, gas
costs, and data­
set complexity



Ethereum
Ropsten
testnet



MNIST and
CIFAR-10



Ethereum
Blockchain



**Fig. 7** Literature map of FL applications (Litmaps 2024)

### **5.1 FL applications in natural language processing**


FL has applications in the domain of NLP that improve text prediction, enhance voice
assistants, and develop personalized translation models. Hard et al. (2018) highlights the
advantages of training recurrent neural network language models directly on client devices,
enabling next-word prediction in a smartphone virtual keyboard without transferring sensi­
tive data. Here, FL environment simplifies the process of implementing privacy by default,
granting customers control over how their data is used. Google’s GBoard is a popular
application that utilizes FL to enhance query suggestions in the Android phone keyboard
(Yang et al. 2018). Emoji can be predicted from text entered on a mobile keyboard using a
word-level recurrent neural network, which has been demonstrated by Ramaswamy et al.
(2019). The work shows that it is possible to train production-quality models for applica­
tions involving natural language interpretation using federated learning while maintaining
user data on their devices. The system (Zhu et al. 2019) for processing financial documents
must have unsegmented text recognition, where the financial documents contain impor­
tant personal data, such as invoices, transcripts, identity certificates, etc. These data are
frequently held on secure servers owned by various organizations, and they must not be

# `1 3`


Exploring privacy mechanisms and metrics in federated learning



Page 29 of 51 **223**



moved past the firewall of the respective institution. Here FL offers a data-secure method for
combining disparate datasets for model training. The FL framework (Liu et al. 2020) aims
to generate various image representations from distinct tasks, which are then integrated to
form highly detailed picture representations for vision-and-language grounding tasks, such
as image captioning and visual question answering (VQA). To learn such visual representa­
tions, the study proposed the Aligning, Integrating and Mapping Network (aimNet). For FL
models, a variable ensemble distillation aggregation technique (Lin et al. 2020) allows for
cost savings, improved privacy, and resilient integration across a range of heterogeneous
client models. However, the only way to directly average model parameters is if all of the
models have the same size and structure, which could be a limitation in a lot of situations.
NLP models are trained in a distributed computing environment using FedNLP (Lin et al.
2021), which efficiently performs NLP tasks like text summarization and sequence tag­
ging. AdaFL (Cai et al. 2023), is an add-on to the FedNLP framework by improving the
adapter configuration over the training session where shallow knowledge is learned rapidly
by training fewer and smaller adapters at the top layers of the model and by assigning
participant devices to trial groups, AdaFL continuously tracks possible adaptor combina­
tions. It demonstrates up to 155.5 times faster model convergence time than vanilla FedNLP.
Hilmkil et al. (2021) looked into FL parameters for transformer language model tuning.
Additionally, three BERT transformer variations- BERT, ALBERT, and DistilBERT-were
assessed for several NLP tasks like text categorization and sentiment analysis. Basu et al.
(2021a) offers a text classification model built upon contextualized transformers (BERT and
Roberta) linked with privacy features like federated learning and differential privacy. They
describe how to analyze desired privacy-utility tradeoffs and how to privately train NLP
models using the Financial Phrase Bank dataset. FL framework named SEFL (Deng et al.
2022), significantly improves performance by eliminating the need for trusted entities like
aggregators or cryptographic primitives. FedBERT (Tian et al. 2022), is a pre-training tool
for BERT that uses split learning with FL to pre-train an extended model with the ability to
function exceptionally well while limiting the disclosure of raw data. The recommendation
system with federated learning (Mansour et al. 2020) is promoted with the development of
a language model that can promptly deliver the next suggestion when users make a request.
Businesses are using chatbots to promote their services, but there are privacy challenges like
access control and data leakage, which need to be addressed. Jalali and Hongsong (2024)
suggests a framework combining FL with Blockchain and FHE with face recognition and
increases chatbot accuracy by 90%. ChatGPT has generated interest in the revolutionary
potential of chatbots to automate language tasks worldwide. Su et al. (2024) leverages the
strengths of DFL and SMPC to create chatbot systems that are both privacy-preserving and
secure. Here, the encrypted global model is stored via IPFS, and clients need to decrypt
using their private keys.

### **5.2 FL applications in healthcare**


Advancements in artificial intelligence has resulted in the creation of numerous applications
that identify and predict diseases, handle medical diagnostic evidence, and also in pharma­
ceutical sectors. However, gathering medical data from several hospitals is challenging due
to patient privacy protections. Therefore, FL is essential to the healthcare industry because
it permits the creation of diagnostic tools and predictive models while protecting patient

# `1 3`


**223** Page 30 of 51



D. Shenoy et al.



privacy and regulatory compliance. Without a central patient database, FL enables coopera­
tive model training across several healthcare facilities. With the development of FL-based
healthcare applications, standards and privacy regulations for electronic medical informa­
tion for patients are required. The various ethical and legal issues currently arising in the
right to patient privacy and ways to improve patient data utilization without compromising
privacy are analyzed in Price and Cohen (2019). Federated learning can improve security
and efficiency when paired with edge computing, Blockchain, and privacy-preserving algo­
rithms. Application of FL in healthcare is broadly classified into 3 categories, namely:


- **Disease prediction:** With FL, medical professionals may create predictive models for
conditions like diabetes, cancer, and heart problems by combining data from many
sources without exchanging patient records. Furthermore, various globally accessible
public medical databases like the Molecular Taxonomy of Breast Cancer International
Consortium (METABRIC), The Cancer Genome Atlas (TCGA), and the National Can­
cer Database (NCDB), have facilitated the development of training algorithms such
as transfer learning, leading to substantial advancements in medical research (Li et al.
2023a). FedHealth (Chen et al. 2020), a federated transfer learning platform for wearable
healthcare, allows precise, individualized healthcare without compromising privacy and
security for Parkinson’s disease diagnosis. Encrypting patient data in memory and disk,
a federated cloud architecture-based rheumatic heart disease classifier (Blanquer et al.
2020) improves privacy without relocating patient data. Basu et al. (2021b) explores the
use of NLP techniques in diagnosing medical conditions like depression, focusing on
the use of differential privacy in FL setup. The research provides insights into private
training of NLP models and offers an open-source implementation for future healthcare
and mental health studies. Elayan et al. (2021) proposed methods to improve healthcare
sustainability IoT-driven platforms for data analysis, preserving privacy and supporting
decentralized data structure, and experimenting with FL algorithms for skin disease
detection. Auto-FedAvg (Xia et al. 2021), is a data-driven technique that automatically
adjusts aggregation weights based on data distributions and models’ training progress.
The technique outperforms FL algorithms on a CIFAR-10 dataset and shows efficiency
in medical image analysis tasks. Collaborative learning of data from different sources
using clustered federated learning for COVID-19 diagnosis is explored (Qayyum et al.
2022), the study promises better results comparing with specialized FL baseline and
multi-modal conventional FL. Hybrid framework developed by Yaqoob et al. (2023) for
heart disease classification, utilizing support vector machines and modified artificial bee
colony optimization, with federated matched averaging for privacy. It addressed train­
ing latency, communication cost, and SPoF. FeSEC (Asad and Yuan 2024), is a secure
and efficient FL framework developed to improve accuracy and privacy preservation in
COVID-19 detection, utilizing sparse compression and homomorphic encryption for
efficient communication among remote hospitals.

- **Medical imaging analysis:** FL increases the precision of diagnostic models without
compromising patient privacy. The creation of image-based diagnostic tools for MRI,
CT, and X-ray imaging modalities is made possible by FL methods. In order to help
clinicians diagnose patients, Lee et al. (2019) employed a multi-federated learning net­
work built on the APOLLO framework to convert real-world data into medical diagnos­
tic evidence. The framework (Silva et al. 2019) offers an application for multi-centric

# `1 3`


Exploring privacy mechanisms and metrics in federated learning



Page 31 of 51 **223**



brain imaging data analysis and uses FL to examine the connections between brain
anatomy and disease using big datasets gathered from various clinics. The open-source
federated learning front-end framework (Silva et al. 2020) addresses data access and
transfer challenges in healthcare. It accommodates various models and optimization
methods and demonstrates the workflow for deploying learning models. The lack of
labeled training sets remains an issue when dealing with medical picture segmentation;
FKD-Med (Sun et al. 2024), is developed for privacy-sensitive data aggregation across
many healthcare institutions with knowledge distillation (KD) to improve communica­
tion efficiency. The study was tested on two medical picture segmentation datasets, and
it improved data privacy, reduced communication costs, and increased accuracy.

- **Pharmaceutical sectors:** Pharmaceutical businesses use collaborative model training
facilitates and dispersed data sources to support drug research and development while
maintaining data privacy. Prediction on the hospitalization rate of heart disease patients
is studied (Brisimi et al. 2018), and a community-based FL model is developed to pre­
dict mortality and ICU stay time (Huang et al. 2019). In order to secure patient data,
Goecks et al. (2020) suggested employing FL in biomedicine to train a model using data
from several healthcare systems. FL addresses various statistical and system challenges
in biomedical science (Xu et al. 2021) by providing access to diverse clinical, pharma­
ceutical, and hospital data while ensuring data privacy.

### **5.3 FL applications in IoT and edge computing**


FL is suitable for preserving privacy on IoT devices since it allows strong privacy controls,
access control, and robust classifiers without compromising sensitive information. FL safe­
guards user privacy and ensures data security throughout the device phase by exchanging
protected limits. It is advised to employ FL in various IoT and edge networks as a reli­
able, intelligent, and efficient alternative to traditional centralized methods (Kholod et al.
2020). The study discusses machine learning’s role in safeguarding IoT networks; the paper
addresses potential risks and threats related to IoT systems. It highlights the application of
FL for IoT security, addressing security threats and performance issues. Various ML models
like neural networks, support vector machines (SVM), and CNNs have been employed,
along with technologies like elliptic-curve cryptography and Blockchain, to enhance pri­
vacy and security in FL. It enables training ML-based intrusion detection systems (IDS) on
distributed IoT devices, improving security against various attack types. FL utilizes cryp­
tographic approaches like homomorphic encryption and secure function evaluation to pre­
serve privacy while exchanging model updates securely. Differential Privacy techniques are
employed in FL to ensure privacy-preserving learning. Federated learning models in IoT
networks are designed to capture compressed representations of anomalous observations,
enhancing privacy protection. FL addresses privacy concerns by securely updating local
and global models to prevent exposure of sensitive user data (Ghimire and Rawat 2022).

To address data and content level privacy issues, Yin et al. (2021) suggests a hybrid
privacy-preserving FL method that blends function encryption techniques with Bayesian
differential privacy techniques. The sparse differential gradient (SDG) is used to address
efficiency concerns. The Local Bayesian Differential Privacy technique adjusts privacy
budget allocation, noise addition, and service quality. During data aggregation, harmful
attempts are prevented by enhanced function encryption. Lu et al. (2020a) combines fed­

# `1 3`


**223** Page 32 of 51



D. Shenoy et al.



erated learning with permissioned Blockchain to offer a privacy-preserving data-sharing
mechanism for multiple distributed parties in IIoT applications. By using multiparty data
retrieval, these permissioned Blockchains takes the place of a trusted curator to connect each
participant. The plan increases computing resource efficiency and usage while strengthening
security without relying on a single source of trust. VFL is a verifiable FL framework for
big data in industrial IoT, which uses Lagrange interpolation to confirm the aggregated gra­
dients are accurate (Fu et al. 2022). The VFL ensures the security of the model and private
gradient, maintaining constant verification overhead regardless of participant count. The
approach prevents malicious aggregation servers from returning forged gradients. The rapid
advancement of IoT and smart services has led to the development of various cyber-physical
systems (CPS), including intelligent connected cars, smart farming, and smart logistics.
Zhang et al. (2020) enables smart services that use trusted federated learning frameworks
for training machine learning models and ensure the services are trustworthy to monitor
the CPS behaviors. PerFit is a cutting-edge cloud-based system that combines data pri­
vacy protection and federated learning for intelligent IoT applications (Wu et al. 2020). It
allows for reduced latency and faster processing capacity by addressing heterogeneity in
models, data statistics, and devices in IoT applications. Imteaj and Amini (2019) proposed
a framework to create sensors for a range of decision-making applications such as tem­
perature, ambient light, and gyroscope sensors etc. Using distributed sensing to create a
system where dispersed devices autonomously activate and collect all available sensor data.
It lowers the cost of adding new sensors for independent Internet of Things applications and
permits remote data collection. To solve the distributed optimization problem with privacy
concerns, the differential privacy and multiparty computation are used. Kwon et al. (2020)
presents a multiagent deep reinforcement learning algorithm for FL in the ocean environ­
ment using Internet-of-Underwater-Things (IoUT) devices. The FL-based distributed deep
learning system aggregates local model parameters from each IoT device to create a global
learning model, ensuring reliable delivery of parameters to a centralized FL machine. The
algorithm aims to enhance deep learning throughput performance by computing efficient
solutions using a MADDOG-based algorithm with fewer iterations. Du et al. (2020) can
improve learning efficiency and privacy by efficiently utilizing network resources of FL in
vehicular IoT systems. It can handle unbalanced data distribution and non-IID data, enhanc­
ing performance. Here FL addresses challenges like mobility, communication bandwidth,
and quality of service (QoS) constraints, leading to more intelligent and efficient systems.
Zhou et al. (2018) proposed a differential FL-based framework for real-time data processing
in multi-robot environments. The proposed framework collects essential data while ensur­
ing data privacy and enabling real-time processing. FL faces challenges with centralized
optimization, relying on a central server, which can lead to network scalability issues and
SPoF. Savazzi et al. (2020) developed a fully distributed FL algorithm that generates data
functionality inside the network, overcoming SPoF. Samarakoon et al. (2019) addresses
the issue of joint power and resource allocation (JPRA) for low-latency communication by
using an FL-based system that combines transmit power and resource allocation in vehicu­
lar networks. Gupta et al. (2022a) designed an approach to improve the security and pri­
vacy of IoT-generated data stored in cloud environments by integrating K-anonymization,
ciphertext-policy attribute-based encryption (CP-ABE), and a voting classifier. The model
ensures data protection, optimizes privacy preservation, and improves processing efficiency.

# `1 3`


Exploring privacy mechanisms and metrics in federated learning



Page 33 of 51 **223**



Edge computing involves processing data locally on edge devices or servers close to the
data source, which helps to minimize latency and decrease bandwidth usage (Singh et al.
2022a). FL enhances edge computing by allowing collaborative model training on these
devices while preserving data privacy. Some applications of FL within edge computing
include:


- **Real-time anomaly detection:** IoT consists of devices connected to the Internet that
produce vast amounts of user data. These data can be exploited by malicious attackers
to steal and manipulate information. To tackle this problem, Mothukuri et al. (2021a)
proposed a FL-based anomaly detection approach, which enhances the accuracy of the
global machine learning model by utilizing federated training cycles on gated recurrent
units (GRUs) models and incorporating updates from multiple sources. According to
experimental results, it performs better at protecting user data privacy and detecting at­
tacks than typical machine learning versions. Through cooperatively analyzing stream­
ing data, FL enables edge devices, like industrial sensors, IoT devices, and surveillance
cameras, to discover anomalies and security concerns in real-time. Federated Learning
reduces communication latency and improves anomaly detection accuracy. Huong et al.
(2021) explores integrating edge computing with smart manufacturing systems to en­
hance real-time data processing and reduce anomaly detection delay, utilizing Federated
Learning for improved cyber threat response. Poorazad et al. (2024) enhances anomaly
detection through synchronous and buffered learning, preserving data privacy through
HE and addressing communication bottlenecks for efficient model training across di­
verse clients.

- **Personalized recommendations:** Using FL approaches, edge devices like smart­
watches, smartphones, and IoT devices can provide consumers with customized recom­
mendations based on their preferences and behavior patterns. Smartwatches and fitness
trackers have become increasingly popular these days as they can detect brain activity,
monitor health factors, and evaluate hydration levels (Iqbal et al. 2021). AI-enabled
smart home devices may learn from human behavior and automate many household
functions (Gill et al. 2022). On edge devices, collaborative model training guarantees
data sovereignty and user privacy. In study (Neumann et al. 2023), lossy quantization
is involved, which not only compresses the data but also acts as a form of parameter
obfuscation, which provides a defense against certain attacks that recreate input data
using model parameters.

- **Autonomous vehicles:** FL trains ML models on edge devices on vehicles, allowing au­
tonomous vehicles to learn and adapt to a variety of road surroundings, traffic patterns,
and driving circumstances. FL protects passenger privacy while improving autonomous
driving systems’ safety and functionality. Samarakoon et al. (2019) explores the prob­
lem of JPRA for ultra-reliable low-latency communication in vehicular networks, em­
phasizing the reduction power consumption of the network while ensuring high reliabil­
ity with respect to probabilistic queuing delays. The safety and privacy of passengers
are seriously threatened by data leaks in vehicular cyber-physical systems (VCPS), par­
ticularly when there are several users and transmission channels. To improve data pri­
vacy, Lu et al. (2020b) suggests a safe architecture, a federated learning mechanism that
protects privacy, and a two-phase mitigation strategy that consists of cooperative data
leakage detection and intelligent data transformation. A federated learning framework

# `1 3`


**223** Page 34 of 51



D. Shenoy et al.



(Pokhrel and Choi 2020b) that prioritizes privacy and communication efficiency and
improves the performance of the Internet of Vehicles (IoV) by training on-vehicle learn­
ing models through the local exchange of inputs, outputs, and learning parameters. Due
to the intermittent and unreliable communications in IoV, the reliability and efficiency
of data sharing need to be enhanced and hence, a federated learning architecture com­
bining locally directed acyclic graph (DAG) and permissioned Blockchain is proposed
by Lu et al. (2020c) to improve security and de