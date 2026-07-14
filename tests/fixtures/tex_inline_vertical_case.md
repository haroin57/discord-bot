概ねその理解ですが、「基底ベクトル」という呼び方だけ修正が必要です。基底は定義上、必ず一次独立です。ここで判定しているのは、係数行列 \(A\) の列ベクトルが一次独立かどうかです。

斉次一次方程式

$$
A\boldsymbol{x}=\boldsymbol{0}
$$

を、\(A\) の列ベクトルを使って書くと、

$$
x_1\boldsymbol{a}_1+x_2\boldsymbol{a}_2+\cdots+x_n\boldsymbol{a}_n
=\boldsymbol{0}
$$

となります。

- 列ベクトル \(\boldsymbol{a}_1,\ldots,\boldsymbol{a}_n\) が一次独立なら、係数は全て \(0\) に限られる。

$$
\boldsymbol{x}=\boldsymbol{0}
$$

つまり、自明解しかありません。

- 列ベクトルが一次従属なら、全てが \(0\) ではない係数の組が存在する。

$$
\boldsymbol{x}\ne\boldsymbol{0}
$$

つまり、非自明解があります。

係数行列が \(m\times n\)、階数が \(r\) の場合、解空間の自由度は階数・退化次数定理から

$$
\dim\ker A=n-r
$$

です。したがって、

$$
\begin{aligned}
r=n
&\iff \text{列ベクトルが一次独立}\\
&\iff \ker A=\{\boldsymbol{0}\}\\
&\iff \text{自明解のみ},\\[4pt]
r<n
&\iff \text{列ベクトルが一次従属}\\
&\iff \dim\ker A>0\\
&\iff \text{非自明解が存在}.
\end{aligned}
$$

正方行列の場合は、さらに

$$
\det A\ne0
\iff \operatorname{rank}A=n
\iff \text{自明解のみ}
$$

となります。

つまり、斉次一次方程式の非自明解の有無を調べることは、係数行列の列ベクトルの一次独立性を調べることと同じです。
